"""
scanner.py - Scans Codex rollout JSONL files and stores usage data in SQLite.

Reads ~/.codex/sessions/**/*.jsonl (one file per thread) plus ~/.codex/session_index.jsonl
(thread titles), and writes token/rate-limit data to ~/.codex/gpt-usage.db.

See docs/DEVELOPMENT_PLAN.md for the verified rollout format and the design rationale behind
every non-obvious choice here (§2 format, §4 schema + scan algorithm).
"""

import json
import os
import glob
import sqlite3
from pathlib import Path

# Single source of truth for the app version (CLI `--version`, dashboard footer).
VERSION = "0.1.0"

CODEX_DIR = Path.home() / ".codex"
SESSIONS_DIR = CODEX_DIR / "sessions"
SESSION_INDEX = CODEX_DIR / "session_index.jsonl"
DB_PATH = Path(os.environ.get("GPT_USAGE_DB", CODEX_DIR / "gpt-usage.db"))

# Higher number = higher priority when choosing a session's primary model. Longest-prefix
# match wins (see _model_priority). codex-auto-review is the bundled review model and ranks
# lowest so it never masks the user's chosen model in the session summary (plan §4.5).
MODEL_PRIORITY = {
    "gpt-5.6": 6,
    "gpt-5.5": 5,
    "gpt-5.4": 4,
    "gpt-5.3": 3,
    "gpt-5": 2,
    "codex-auto-review": 1,
}

# event_msg payload types we consume. Everything else (agent_message, agent_reasoning,
# task_started, exec_command_end, context_compacted, turn_aborted, thread_settings_applied,
# ...) is skipped silently — see the Phase 0 census in plan §2.4.
_TOKEN_COUNT = "token_count"


def _model_priority(model):
    """Return a priority score for a model name (higher = more capable). Longest prefix wins."""
    if not model:
        return 0
    m = model.lower()
    best = 0
    best_len = -1
    for keyword, priority in MODEL_PRIORITY.items():
        if m.startswith(keyword) and len(keyword) > best_len:
            best = priority
            best_len = len(keyword)
    return best


# ── DB setup ────────────────────────────────────────────────────────────────

def get_db(db_path=DB_PATH):
    # Ensure the parent dir exists — on a fresh install ~/.codex should already be there, but
    # a custom GPT_USAGE_DB path (or CI) may not be, and sqlite3.connect needs the parent dir.
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id        TEXT PRIMARY KEY,
            parent_thread_id  TEXT,
            thread_source     TEXT,
            originator        TEXT,
            cli_version       TEXT,
            project_name      TEXT,
            cwd               TEXT,
            first_timestamp   TEXT,
            last_timestamp    TEXT,
            model             TEXT,
            effort            TEXT,
            turn_count        INTEGER DEFAULT 0,
            total_input_tokens     INTEGER DEFAULT 0,
            total_cached_input     INTEGER DEFAULT 0,
            total_output_tokens    INTEGER DEFAULT 0,
            total_reasoning_tokens INTEGER DEFAULT 0,
            topic             TEXT
        );

        CREATE TABLE IF NOT EXISTS turns (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id        TEXT,
            timestamp         TEXT,
            model             TEXT,
            effort            TEXT,
            input_tokens            INTEGER DEFAULT 0,
            cached_input_tokens     INTEGER DEFAULT 0,
            output_tokens           INTEGER DEFAULT 0,
            reasoning_output_tokens INTEGER DEFAULT 0,
            is_subagent       INTEGER DEFAULT 0,
            tool_name         TEXT
        );

        CREATE TABLE IF NOT EXISTS rate_limit_snapshots (
            bucket            TEXT PRIMARY KEY,   -- 15-min slot key (downsampling, plan §4.1)
            timestamp         TEXT,
            plan_type         TEXT,
            primary_used_percent   REAL,
            primary_window_minutes INTEGER,
            primary_resets_at      INTEGER,
            secondary_used_percent   REAL,
            secondary_window_minutes INTEGER,
            secondary_resets_at      INTEGER
        );

        CREATE TABLE IF NOT EXISTS processed_files (
            path        TEXT PRIMARY KEY,
            mtime       REAL,
            lines       INTEGER,
            session_id  TEXT,   -- the single thread id this file belongs to (§4.3)
            last_model  TEXT,   -- carry-forward parser state across incremental scans (§4.3)
            last_effort TEXT
        );

        CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT);

        CREATE INDEX IF NOT EXISTS idx_turns_session   ON turns(session_id);
        CREATE INDEX IF NOT EXISTS idx_turns_timestamp ON turns(timestamp);
        CREATE INDEX IF NOT EXISTS idx_sessions_first  ON sessions(first_timestamp);
        CREATE INDEX IF NOT EXISTS idx_rls_timestamp   ON rate_limit_snapshots(timestamp);
    """)
    # Additive migrations for DBs created by an earlier build (idempotent).
    _ensure_column(conn, "processed_files", "session_id", "TEXT")
    _ensure_column(conn, "processed_files", "last_model", "TEXT")
    _ensure_column(conn, "processed_files", "last_effort", "TEXT")
    conn.commit()


def _ensure_column(conn, table, column, decl):
    """Add a column to an existing table if it isn't already present."""
    # PRAGMA table_info columns: (cid, name, type, ...). Index by position (1 = name) so this
    # works whether or not the caller set a Row factory.
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        return True
    return False


# ── Helpers ─────────────────────────────────────────────────────────────────

def project_name_from_cwd(cwd):
    """Derive a friendly project name from a cwd path (last 2 components).

    Codex cwd values on Windows contain backslashes — normalize first.
    """
    if not cwd:
        return "unknown"
    parts = cwd.replace("\\", "/").rstrip("/").split("/")
    if len(parts) >= 2:
        return "/".join(parts[-2:])
    return parts[-1] if parts else "unknown"


def _rate_bucket(timestamp):
    """15-minute slot key from an ISO timestamp (YYYY-MM-DDTHH:Q). Empty ts -> ''."""
    # timestamp like "2026-07-15T15:01:11.003Z"
    if not timestamp or len(timestamp) < 16:
        return timestamp or ""
    try:
        minute = int(timestamp[14:16])
    except ValueError:
        return timestamp
    return f"{timestamp[:13]}:{minute // 15}"


def load_session_titles(index_path=SESSION_INDEX):
    """Read session_index.jsonl → {thread_id: thread_name}. Missing file -> {}."""
    titles = {}
    p = Path(index_path)
    if not p.exists():
        return titles
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                tid = rec.get("id")
                name = rec.get("thread_name")
                if tid and name:
                    titles[tid] = name
    except Exception as e:
        print(f"  Warning: error reading {index_path}: {e}")
    return titles


# ── Parsing ─────────────────────────────────────────────────────────────────

def _blank_meta(session_id):
    return {
        "session_id": session_id,
        "parent_thread_id": None,
        "thread_source": None,
        "originator": None,
        "cli_version": None,
        "project_name": "unknown",
        "cwd": None,
        "first_timestamp": "",
        "last_timestamp": "",
        "model": None,
        "effort": None,
    }


def _parse_lines(lines, current_model=None, current_effort=None, skip_lines=0, active_id=None):
    """Parse an iterable of rollout lines.

    Carries `current_model`/`current_effort` forward from turn_context records and stamps them
    onto token_count turns (the model is never on the usage record — plan §2.2). Multiple
    session_meta records with the same id are merged, not treated as new sessions (§4.3).
    `active_id` seeds session attribution for incremental passes that skip past the file's
    session_meta line (one rollout file = one thread id — Phase 0).

    Returns (session_metas dict, turns list, rate_snapshots list, line_count,
             last_model, last_effort).
    """
    session_meta = {}   # session_id -> meta dict
    turns = []
    snapshots = []
    line_count = 0

    for line_count, raw in enumerate(lines, 1):
        if line_count <= skip_lines:
            continue
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue

        rtype = rec.get("type")
        payload = rec.get("payload") or {}
        timestamp = rec.get("timestamp", "")

        if rtype == "session_meta":
            sid = payload.get("id")  # always use id, not session_id (§2.3)
            if not sid:
                continue
            active_id = sid
            meta = session_meta.get(sid) or _blank_meta(sid)
            cwd = payload.get("cwd")
            meta["parent_thread_id"] = payload.get("parent_thread_id") or meta["parent_thread_id"]
            meta["thread_source"] = payload.get("thread_source") or meta["thread_source"]
            meta["originator"] = payload.get("originator") or meta["originator"]
            meta["cli_version"] = payload.get("cli_version") or meta["cli_version"]
            if cwd:
                meta["cwd"] = cwd
                meta["project_name"] = project_name_from_cwd(cwd)
            mts = payload.get("timestamp") or timestamp
            if mts and (not meta["first_timestamp"] or mts < meta["first_timestamp"]):
                meta["first_timestamp"] = mts
            if mts and (not meta["last_timestamp"] or mts > meta["last_timestamp"]):
                meta["last_timestamp"] = mts
            session_meta[sid] = meta
            continue

        if rtype == "turn_context":
            model = payload.get("model")
            if model:
                current_model = model
            eff = payload.get("effort")
            if eff:
                current_effort = eff
            # a cd mid-session updates cwd/project on the active session
            cwd = payload.get("cwd")
            if cwd and active_id and active_id in session_meta:
                session_meta[active_id]["cwd"] = cwd
                session_meta[active_id]["project_name"] = project_name_from_cwd(cwd)
            continue

        if rtype == "event_msg" and payload.get("type") == _TOKEN_COUNT:
            info = payload.get("info")
            rl = payload.get("rate_limits")

            if isinstance(info, dict):
                last = info.get("last_token_usage") or {}
                inp = last.get("input_tokens", 0) or 0
                cached = last.get("cached_input_tokens", 0) or 0
                out = last.get("output_tokens", 0) or 0
                reasoning = last.get("reasoning_output_tokens", 0) or 0
                # Skip all-zero rows (nothing billable/observable).
                if (inp + out) > 0 and active_id:
                    meta = session_meta.get(active_id)
                    is_sub = 1 if (meta and meta.get("thread_source") not in (None, "user")) else 0
                    turns.append({
                        "session_id": active_id,
                        "timestamp": timestamp,
                        "model": current_model,
                        "effort": current_effort,
                        "input_tokens": inp,
                        "cached_input_tokens": cached,
                        "output_tokens": out,
                        "reasoning_output_tokens": reasoning,
                        "is_subagent": is_sub,
                        "tool_name": None,
                    })
                    if timestamp and meta:
                        if not meta["last_timestamp"] or timestamp > meta["last_timestamp"]:
                            meta["last_timestamp"] = timestamp
                        if not meta["first_timestamp"] or timestamp < meta["first_timestamp"]:
                            meta["first_timestamp"] = timestamp

            if isinstance(rl, dict):
                snap = _extract_snapshot(timestamp, rl)
                if snap:
                    snapshots.append(snap)
            continue

        # Unknown record type -> skip silently.

    return session_meta, turns, snapshots, line_count, current_model, current_effort


def _extract_snapshot(timestamp, rl):
    """Build a rate_limit_snapshots row from a rate_limits block (defensive .get())."""
    prim = rl.get("primary") or {}
    sec = rl.get("secondary") or {}
    if not isinstance(prim, dict):
        prim = {}
    if not isinstance(sec, dict):
        sec = {}
    return {
        "bucket": _rate_bucket(timestamp),
        "timestamp": timestamp,
        "plan_type": rl.get("plan_type"),
        "primary_used_percent": prim.get("used_percent"),
        "primary_window_minutes": prim.get("window_minutes"),
        "primary_resets_at": prim.get("resets_at"),
        "secondary_used_percent": sec.get("used_percent"),
        "secondary_window_minutes": sec.get("window_minutes"),
        "secondary_resets_at": sec.get("resets_at"),
    }


def parse_rollout_file(filepath, current_model=None, current_effort=None, skip_lines=0,
                       active_id=None):
    """Parse a rollout file (optionally only lines past skip_lines, seeded with carry state)."""
    try:
        with open(filepath, encoding="utf-8", errors="replace") as f:
            return _parse_lines(f, current_model, current_effort, skip_lines, active_id)
    except Exception as e:
        print(f"  Warning: error reading {filepath}: {e}")
        return {}, [], [], 0, current_model, current_effort


# ── DB writes ───────────────────────────────────────────────────────────────

def upsert_sessions(conn, metas, titles):
    """Insert/merge session rows. Token totals are reconciled from turns later (§4.4)."""
    for m in metas:
        sid = m["session_id"]
        if not m.get("first_timestamp"):
            # A session with no observed activity timestamp — skip phantom rows.
            existing = conn.execute(
                "SELECT 1 FROM sessions WHERE session_id = ?", (sid,)).fetchone()
            if existing is None:
                continue
        topic = titles.get(sid)
        row = conn.execute(
            "SELECT model, effort, first_timestamp, topic FROM sessions WHERE session_id = ?",
            (sid,)).fetchone()
        if row is None:
            conn.execute("""
                INSERT INTO sessions
                    (session_id, parent_thread_id, thread_source, originator, cli_version,
                     project_name, cwd, first_timestamp, last_timestamp, model, effort,
                     turn_count, total_input_tokens, total_cached_input, total_output_tokens,
                     total_reasoning_tokens, topic)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0, 0, ?)
            """, (sid, m["parent_thread_id"], m["thread_source"], m["originator"],
                  m["cli_version"], m["project_name"], m["cwd"], m["first_timestamp"],
                  m["last_timestamp"], m["model"], m["effort"], topic))
        else:
            # Keep the highest-priority model across incremental updates (§4.5).
            existing_model = row["model"]
            model_to_set = m["model"] if _model_priority(m["model"]) > _model_priority(existing_model) else existing_model
            first_ts = row["first_timestamp"]
            if m["first_timestamp"] and (not first_ts or m["first_timestamp"] < first_ts):
                first_ts = m["first_timestamp"]
            conn.execute("""
                UPDATE sessions SET
                    parent_thread_id = COALESCE(?, parent_thread_id),
                    thread_source    = COALESCE(?, thread_source),
                    originator       = COALESCE(?, originator),
                    cli_version      = COALESCE(?, cli_version),
                    project_name     = CASE WHEN ? != 'unknown' THEN ? ELSE project_name END,
                    cwd              = COALESCE(?, cwd),
                    first_timestamp  = ?,
                    last_timestamp   = MAX(last_timestamp, ?),
                    model            = ?,
                    effort           = COALESCE(?, effort),
                    topic            = COALESCE(?, topic)
                WHERE session_id = ?
            """, (m["parent_thread_id"], m["thread_source"], m["originator"], m["cli_version"],
                  m["project_name"], m["project_name"], m["cwd"], first_ts,
                  m["last_timestamp"] or "", model_to_set, m["effort"], topic, sid))


def insert_turns(conn, turns):
    conn.executemany("""
        INSERT INTO turns
            (session_id, timestamp, model, effort, input_tokens, cached_input_tokens,
             output_tokens, reasoning_output_tokens, is_subagent, tool_name)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        (t["session_id"], t["timestamp"], t["model"], t["effort"],
         t["input_tokens"], t["cached_input_tokens"], t["output_tokens"],
         t["reasoning_output_tokens"], t["is_subagent"], t["tool_name"])
        for t in turns
    ])


def insert_snapshots(conn, snapshots):
    """Downsampled insert: one row per 15-min bucket (INSERT OR IGNORE on bucket PK)."""
    conn.executemany("""
        INSERT OR IGNORE INTO rate_limit_snapshots
            (bucket, timestamp, plan_type, primary_used_percent, primary_window_minutes,
             primary_resets_at, secondary_used_percent, secondary_window_minutes,
             secondary_resets_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        (s["bucket"], s["timestamp"], s["plan_type"], s["primary_used_percent"],
         s["primary_window_minutes"], s["primary_resets_at"], s["secondary_used_percent"],
         s["secondary_window_minutes"], s["secondary_resets_at"])
        for s in snapshots if s["bucket"]
    ])


def _reconcile_sessions(conn):
    """Recompute session token/turn totals and primary model from the turns table (§4.4/§4.5)."""
    conn.execute("""
        UPDATE sessions SET
            total_input_tokens     = COALESCE((SELECT SUM(input_tokens) FROM turns WHERE turns.session_id = sessions.session_id), 0),
            total_cached_input     = COALESCE((SELECT SUM(cached_input_tokens) FROM turns WHERE turns.session_id = sessions.session_id), 0),
            total_output_tokens    = COALESCE((SELECT SUM(output_tokens) FROM turns WHERE turns.session_id = sessions.session_id), 0),
            total_reasoning_tokens = COALESCE((SELECT SUM(reasoning_output_tokens) FROM turns WHERE turns.session_id = sessions.session_id), 0),
            turn_count             = COALESCE((SELECT COUNT(*) FROM turns WHERE turns.session_id = sessions.session_id), 0)
    """)
    # Primary model per session = the highest-priority model across its turns (§4.5). Done in
    # Python since the priority order isn't expressible in plain SQL. Sessions with no
    # non-null turn model are left as-is.
    rows = conn.execute(
        "SELECT session_id, model FROM turns WHERE model IS NOT NULL").fetchall()
    best = {}  # session_id -> (priority, model)
    for r in rows:
        sid, model = r["session_id"], r["model"]
        pr = _model_priority(model)
        if sid not in best or pr > best[sid][0]:
            best[sid] = (pr, model)
    for sid, (_pr, model) in best.items():
        conn.execute("UPDATE sessions SET model = ? WHERE session_id = ?", (model, sid))


# ── Scan ────────────────────────────────────────────────────────────────────

def scan(sessions_dir=None, db_path=DB_PATH, index_path=SESSION_INDEX, verbose=True):
    conn = get_db(db_path)
    init_db(conn)

    d = Path(sessions_dir) if sessions_dir else SESSIONS_DIR
    titles = load_session_titles(index_path)

    if not d.exists():
        if verbose:
            print(f"No sessions directory at {d}")
        conn.close()
        return {"new": 0, "updated": 0, "skipped": 0, "turns": 0, "sessions": 0}

    if verbose:
        print(f"Scanning {d} ...")
    files = sorted(glob.glob(str(d / "**" / "*.jsonl"), recursive=True))

    new_files = updated_files = skipped_files = 0
    total_turns = 0
    touched_sessions = set()
    changed = False

    for filepath in files:
        try:
            mtime = os.path.getmtime(filepath)
        except OSError:
            continue

        row = conn.execute(
            "SELECT mtime, lines, session_id, last_model, last_effort "
            "FROM processed_files WHERE path = ?", (filepath,)).fetchone()

        if row and abs(row["mtime"] - mtime) < 0.01:
            skipped_files += 1
            continue

        is_new = row is None
        old_lines = 0 if is_new else (row["lines"] or 0)
        seed_model = None if is_new else row["last_model"]
        seed_effort = None if is_new else row["last_effort"]
        seed_id = None if is_new else row["session_id"]

        if verbose:
            print(f"  [{'NEW' if is_new else 'UPD'}] {filepath}")

        metas, turns, snaps, line_count, last_model, last_effort = parse_rollout_file(
            filepath, current_model=seed_model, current_effort=seed_effort,
            skip_lines=old_lines, active_id=seed_id)

        if line_count < old_lines:
            # File shrank (unexpected — append-only per Phase 0). Full reparse for safety:
            # drop this file's sessions' turns and re-read from scratch.
            if verbose:
                print("    file shrank; full reparse")
            for sid in _session_ids_in_file(filepath):
                conn.execute("DELETE FROM turns WHERE session_id = ?", (sid,))
            metas, turns, snaps, line_count, last_model, last_effort = parse_rollout_file(
                filepath, current_model=None, current_effort=None, skip_lines=0)

        # One file = one thread id. Prefer a freshly-seen session_meta id, else the seed.
        file_sid = next(iter(metas), None) or seed_id

        if turns or metas or snaps:
            upsert_sessions(conn, list(metas.values()), titles)
            insert_turns(conn, turns)
            insert_snapshots(conn, snaps)
            for sid in metas:
                touched_sessions.add(sid)
            for t in turns:
                touched_sessions.add(t["session_id"])
            total_turns += len(turns)
            changed = True

        conn.execute("""
            INSERT OR REPLACE INTO processed_files
                (path, mtime, lines, session_id, last_model, last_effort)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (filepath, mtime, line_count, file_sid, last_model, last_effort))
        conn.commit()

        if is_new:
            new_files += 1
        else:
            updated_files += 1

    if changed:
        _reconcile_sessions(conn)
        conn.commit()

    if verbose:
        print("\nScan complete:")
        print(f"  New files:     {new_files}")
        print(f"  Updated files: {updated_files}")
        print(f"  Skipped files: {skipped_files}")
        print(f"  Turns added:   {total_turns}")
        print(f"  Sessions seen: {len(touched_sessions)}")

    conn.close()
    return {"new": new_files, "updated": updated_files, "skipped": skipped_files,
            "turns": total_turns, "sessions": len(touched_sessions)}


def _session_ids_in_file(filepath):
    """Session ids appearing as session_meta in a file (for shrink-fallback cleanup)."""
    ids = set()
    try:
        with open(filepath, encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"session_meta"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") == "session_meta":
                    sid = (rec.get("payload") or {}).get("id")
                    if sid:
                        ids.add(sid)
    except Exception:
        pass
    return ids


if __name__ == "__main__":
    import sys
    sdir = None
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--sessions-dir" and i + 1 < len(args):
            sdir = args[i + 1]
    scan(sessions_dir=sdir)

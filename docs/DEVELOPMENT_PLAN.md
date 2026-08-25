# gpt-usage — Development Plan & Architecture

A local usage dashboard for **Codex / GPT models** (OpenAI's coding agent), modeled on the proven
architecture of [claude-usage](https://github.com/phuryn/claude-usage) (v1.5.5 is the reference
implementation, cloned at `../claude-usage-1.5.5`).

**Design contract (non-negotiable):**

- **Three Python files** — `scanner.py`, `cli.py`, `dashboard.py`.
- **Standard library only.** No pip install, no venv, no build step, no API keys.
- **Python 3.8+**, works identically on Windows (`python`) and macOS/Linux (`python3`).
- Run from a terminal: `python cli.py dashboard` and you're done.

This document is the single source of truth for AI agents building the tool. Every schema fact
below was **verified against real rollout files on this machine** (63 files, Codex CLI versions
0.98.0 → 0.144.2, Feb–Jul 2026). Phase 0 ran a read-only census probe over all 63 files and
resolved the open questions; results are recorded in §2.4. Remaining **[VERIFY]** tags are
pricing-only and belong to Phase 2 (they need live openai.com pricing, not local data).

---

## 1. Goals and non-goals

### Goals

1. Read Codex's local session logs (no API, no network) and produce token/cost/usage analytics.
2. Terminal summaries (`today`, `week`, `stats`) and a browser dashboard (`dashboard`).
3. Per-model, per-project, per-day, and subagent attribution.
4. **Rate-limit history** — Codex logs plan type and rolling-window usage percentages with every
   response. Charting "how close am I to my weekly limit over time" is a headline feature that
   the Claude equivalent cannot offer (Claude Code doesn't log this).
5. Incremental scanning — re-scans are near-instant on unchanged files.

### Non-goals

- **ChatGPT web/desktop chat conversations.** These are stored server-side; there is no local
  token-usage log to read. Only Codex surfaces (CLI, VS Code extension, Codex Desktop / Codex
  Work Desktop) write local rollout files. State this clearly in the README.
- Sending anything anywhere. The tool never writes outside its own DB file and never makes
  network requests (the dashboard page loads Chart.js from CDN in the browser — the Python
  process itself stays offline).
- Modifying anything under `~/.codex` other than creating our own DB file.

---

## 2. Source data: the Codex rollout format (verified)

### 2.1 File layout

```
~/.codex/
├── sessions/YYYY/MM/DD/rollout-<ISO-ts>-<uuid>.jsonl   ← one file per thread (the data source)
├── session_index.jsonl                                  ← {id, thread_name, updated_at} per thread
├── config.toml, auth.json, ...                          ← ignore
└── (various sqlite files — internal Codex state; do not touch)
```

- One rollout file per **thread**. Subagent threads and automation (scheduled-task) threads get
  **their own rollout files**, linked to the parent via `parent_thread_id`.
- The file name embeds the thread UUID; the same UUID appears in `session_meta.payload.id`.
- `session_index.jsonl` maps thread id → human-readable `thread_name` (session title). Small
  file; re-read it fully on every scan (no incremental tracking needed).

### 2.2 Record shapes (JSONL, one JSON object per line)

Every line: `{"timestamp": "<ISO-8601 UTC>", "type": "<record_type>", "payload": {...}}`

**`session_meta`** — always the first line of a rollout file:

| Field | Notes |
|---|---|
| `payload.id` | thread UUID — present in **all** versions; use this as `session_id` |
| `payload.session_id` | duplicate of `id`, only in newer versions — do not rely on it |
| `payload.parent_thread_id` | only on subagent threads; links to parent session |
| `payload.timestamp` | thread start time |
| `payload.cwd` | working directory → project name |
| `payload.originator` | surface: observed values `codex_vscode`, `Codex Desktop`, `codex_work_desktop` |
| `payload.cli_version` | e.g. `0.144.2` — store it; useful for debugging schema drift |
| `payload.source` | **string** (`"vscode"`) in some versions, **object** (`{"subagent": {...}}`) in others — must handle both types |
| `payload.thread_source` | `user` \| `subagent` \| `automation` (observed split on this machine: 23/23/5) |
| `payload.model_provider` | `openai` |
| `payload.base_instructions` | huge text blob — ignore, never store |

**`turn_context`** — emitted at the start of each turn; carries the model:

| Field | Notes |
|---|---|
| `payload.model` | e.g. `gpt-5.5`, `gpt-5.4`, `gpt-5.3-codex`, `gpt-5.6-sol`, `gpt-5.6-terra`, `codex-auto-review` (all observed locally) |
| `payload.effort` | reasoning effort (`medium`, etc.) — store it; nice dashboard dimension |
| `payload.cwd` | may differ from session_meta (cd during session) |
| `payload.turn_id` | turn UUID |

The model is **not** on the usage records — the scanner must carry forward the most recent
`turn_context.model` and stamp it onto subsequent token events. A session can change models
mid-thread, so per-event attribution matters (same principle as claude-usage: cost is computed
per turn, never from session aggregates).

**`event_msg` with `payload.type == "token_count"`** — the usage record, one per API response:

```json
{"timestamp":"...","type":"event_msg","payload":{"type":"token_count","info":{
  "total_token_usage":{"input_tokens":45435,"cached_input_tokens":27136,
                       "output_tokens":622,"reasoning_output_tokens":123,"total_tokens":46057},
  "last_token_usage":{"input_tokens":27560,"cached_input_tokens":17152,
                      "output_tokens":387,"reasoning_output_tokens":60,"total_tokens":27947},
  "model_context_window":258400},
 "rate_limits":{"limit_id":"codex","primary":{"used_percent":5.0,"window_minutes":10080,
  "resets_at":1784732025},"secondary":null,"plan_type":"plus", "...":"..."}}}
```

Verified semantics (checked arithmetic on real files):

- `last_token_usage` = tokens for **this** API response; `total_token_usage` = cumulative for
  the thread. Verified: successive totals differ by exactly the next `last_token_usage`
  (17,875 + 27,560 = 45,435 ✓). **Record one turn per token_count event using
  `last_token_usage`** — no cross-event summing of totals, ever.
- **`input_tokens` INCLUDES `cached_input_tokens`** (OpenAI semantics — the opposite of
  Anthropic, where cache tokens are separate fields). Verified: `total_tokens` =
  `input_tokens` + `output_tokens` (17,875 + 235 = 18,110 ✓).
- **`reasoning_output_tokens` is a subset of `output_tokens`**, not additive.
- **`info` can be `null`** (observed in 0.98.0-era files) — skip token accounting for those
  records but still harvest `rate_limits` if present.
- `rate_limits` shape drifts: older files have `primary`/`secondary`/`credits` with
  `plan_type: null` and no `limit_id`; newer files add `limit_id`, `limit_name`, `plan_type`
  (`"plus"`), `individual_limit`. Read every field defensively with `.get()`.
- There is **no message id** on token_count events. Dedup relies on incremental line tracking
  (see §4.3) plus the per-session reconciliation check (see §4.4).

**`response_item`** — full message/tool-call content. Only one use for us: records with
`payload.type == "function_call"` carry `payload.name` (tool name) for optional tool-usage
stats. Everything else in `response_item` (message text) is **ignored and never stored** —
this tool stores token counts and metadata only, never conversation content. That is a privacy
guarantee worth a README sentence.

### 2.3 Known schema-drift traps (all observed locally)

1. `payload.source` is a string or an object depending on version — never assume type.
2. `token_count` with `info: null` exists — guard every access.
3. `session_meta.payload.session_id` is missing in old files — always use `payload.id`.
4. `rate_limits.plan_type` can be `null` or absent.
5. Rollout files start with an enormous `base_instructions` line (10KB+) — parse line-by-line,
   never load whole file into memory as one string.
6. Future Codex versions will drift further — unknown record types and unknown payload fields
   must be silently skipped, never crash the scan.

### 2.4 Phase 0 census (verified against all 63 local files)

Read-only probe results — the empirical basis for the parser's skip-list and invariants.

**Top-level record types seen:** `session_meta`, `turn_context`, `event_msg`, `response_item`,
plus (0.144.2 only) `world_state`, `compacted`. Only `session_meta`, `turn_context`, and
`event_msg`/`token_count` are consumed; **everything else is skipped silently.**

**`event_msg` payload types seen** (only `token_count` is consumed; the rest are the explicit
skip-list): `task_started`, `task_complete`, `turn_aborted`, `user_message`, `agent_message`,
`agent_reasoning`, `token_count`, `exec_command_end`, `patch_apply_end`, `web_search_end`,
`mcp_tool_call_end`, `thread_name_updated`, `thread_settings_applied`, `context_compacted`.

- **`thread_name_updated`** is an in-file session-title event (analogous to claude-usage's
  `custom-title`). It is an *optional* second source of `topic` alongside `session_index.jsonl`;
  `session_index.jsonl` remains the primary source. Low priority — wire it only if convenient.
- **`context_compacted` / `compacted`** = context-window compaction. Present in 3 files.
  Critically, `total_token_usage` **did not reset** in any of them (see §10) — compaction is
  informational for us, not an accounting hazard.

**`source` python type:** `str` ×82, `dict` ×24 (counts exceed 63 because resumes re-emit
`session_meta`). Both must be handled.

**`token_count.info`:** `null` ×11, present ×2747. Guard every access.

**`rate_limits` keys observed:** `primary`, `secondary`, `credits`, `plan_type`, `limit_id`,
`limit_name`, `rate_limit_reached_type`, `individual_limit` (the last four absent in older
files). **`rate_limits.primary` keys:** `used_percent`, `window_minutes`, `resets_at` (stable
across all versions). **`plan_type`:** `null` ×174 or `'plus'` ×2584.

**Session/file relationship:** append-only, resumes re-emit same-id `session_meta`, totals
monotonic — full detail in §4.3.

---

## 3. Architecture

Mirror claude-usage exactly, adapted for the Codex data model:

```
~/.codex/sessions/**/*.jsonl ──→ scanner.parse_rollout_file()
~/.codex/session_index.jsonl ──→ scanner.load_session_titles()
                                       │
                     aggregate_sessions() → upsert_sessions() + insert_turns()
                                       │            + insert_rate_limit_snapshots()
                                       ▼
                     ~/.codex/gpt-usage.db  (SQLite, env override GPT_USAGE_DB)
                                       │
              cli.py queries  ◄────────┴────────►  dashboard.py  GET /api/data
```

| File | Responsibility |
|---|---|
| `scanner.py` | Parse rollouts → SQLite. Owns `VERSION`, DB schema, incremental scan logic. |
| `cli.py` | `scan` / `today` / `week` / `stats` / `dashboard` commands. Owns the `PRICING` dict (Python side). |
| `dashboard.py` | `http.server` serving one embedded HTML/JS page + `GET /api/data` + `POST /api/rescan`. Owns the JS `PRICING` const (must stay in sync with cli.py — copy claude-usage's paired-pricing convention and its test). |

Decisions (settled — do not relitigate during implementation):

- **DB path:** `~/.codex/gpt-usage.db` (namespaced filename so a future Codex-internal
  `usage.db` can't collide). Env override `GPT_USAGE_DB`. Create parent dir defensively.
- **Default port: 8090** — deliberately different from claude-usage's 8080 so both dashboards
  run side by side. `--host/--port` flags and `HOST`/`PORT` env vars, same as claude-usage.
- **Scan target:** `~/.codex/sessions/` only, `--sessions-dir PATH` override (mirrors
  `--projects-dir`).
- **Timestamps** stored as the ISO-8601 UTC strings from the log (claude-usage does the same);
  daily bucketing via `substr(timestamp, 1, 10)`. Dashboard may convert to local time in JS.

---

## 4. SQLite schema and scan algorithm

### 4.1 Tables

```sql
CREATE TABLE IF NOT EXISTS sessions (
    session_id        TEXT PRIMARY KEY,   -- session_meta.payload.id
    parent_thread_id  TEXT,               -- NULL for user threads
    thread_source     TEXT,               -- user | subagent | automation
    originator        TEXT,               -- codex_vscode | Codex Desktop | ...
    cli_version       TEXT,
    project_name      TEXT,               -- derived from cwd (last 2 path components)
    cwd               TEXT,
    first_timestamp   TEXT,
    last_timestamp    TEXT,
    model             TEXT,               -- primary model (priority rule, §4.5)
    effort            TEXT,               -- last seen reasoning effort
    turn_count        INTEGER DEFAULT 0,
    total_input_tokens     INTEGER DEFAULT 0,
    total_cached_input     INTEGER DEFAULT 0,
    total_output_tokens    INTEGER DEFAULT 0,
    total_reasoning_tokens INTEGER DEFAULT 0,
    topic             TEXT                -- thread_name from session_index.jsonl
);

CREATE TABLE IF NOT EXISTS turns (          -- one row per token_count event
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id        TEXT,
    timestamp         TEXT,
    model             TEXT,               -- most recent turn_context.model
    effort            TEXT,
    input_tokens      INTEGER DEFAULT 0,  -- INCLUDES cached (OpenAI semantics)
    cached_input_tokens    INTEGER DEFAULT 0,
    output_tokens     INTEGER DEFAULT 0,  -- INCLUDES reasoning
    reasoning_output_tokens INTEGER DEFAULT 0,
    is_subagent       INTEGER DEFAULT 0,  -- thread_source != 'user'
    tool_name         TEXT                -- best-effort, may stay NULL in v1
);

CREATE TABLE IF NOT EXISTS rate_limit_snapshots (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp         TEXT,
    plan_type         TEXT,
    primary_used_percent   REAL,
    primary_window_minutes INTEGER,
    primary_resets_at      INTEGER,       -- unix epoch
    secondary_used_percent   REAL,
    secondary_window_minutes INTEGER,
    secondary_resets_at      INTEGER
);

CREATE TABLE IF NOT EXISTS processed_files (
    path   TEXT PRIMARY KEY,
    mtime  REAL,
    lines  INTEGER
);

CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT);

CREATE INDEX IF NOT EXISTS idx_turns_session   ON turns(session_id);
CREATE INDEX IF NOT EXISTS idx_turns_timestamp ON turns(timestamp);
CREATE INDEX IF NOT EXISTS idx_sessions_first  ON sessions(first_timestamp);
CREATE INDEX IF NOT EXISTS idx_rls_timestamp   ON rate_limit_snapshots(timestamp);
```

Migration policy: copy claude-usage's `_ensure_column()` additive-migration helper verbatim.
`init_db` must be idempotent and safe to run from read paths (cli/dashboard call it before
querying, exactly like claude-usage's `require_db`).

Rate-limit snapshot volume: one token_count per API response could mean thousands of snapshot
rows per day. **Downsample at insert time:** keep at most one snapshot per (plan window, 15-minute
bucket); the dashboard only needs trend resolution. Implement as a simple "skip insert if last
kept snapshot for the same window is < 15 min older" check during parsing.

### 4.2 Parsing one rollout file

```
current_model = None; current_effort = None
for each line:                       # stream, never slurp
    json.loads → skip on JSONDecodeError
    session_meta  → capture session fields (id, parent_thread_id, thread_source,
                    originator, cli_version, cwd, timestamp; source may be str|dict)
    turn_context  → current_model = payload.model; current_effort = payload.effort
                    (also update cwd if changed)
    event_msg/token_count:
        if payload.info and payload.info.last_token_usage:
            emit turn row from last_token_usage, stamped with current_model/effort
            (skip rows whose token fields are all zero)
        if payload.rate_limits: maybe-emit downsampled rate_limit snapshot
    anything else → skip silently
```

Fallback: if a rollout file has token_count events but **no** turn_context, model stays `NULL`
→ displayed as `unknown`, costed as n/a. **Phase 0 census: 0 of 63 files hit this** — every
file with token_count also has turn_context, across all 9 cli_versions. Keep the fallback as
pure defensive coding, not an expected path.

### 4.3 Incremental scanning

Copy claude-usage's mechanism unchanged — it is battle-tested:

- `processed_files(path, mtime, lines)`; skip file when mtime matches (±0.01s).
- If mtime changed and file grew: reparse **only lines past the stored count**. Critical
  subtlety: the "new lines only" pass won't see the file's `session_meta` or earlier
  `turn_context` records. Solution: persist per-session parser state — store the session row
  on first parse, and on incremental passes look up `current_model`/`effort` from the session
  row (`sessions.model` may have been overwritten by the priority rule, so store the *last
  seen* model separately in `schema_meta` or a small `parse_state` column; simplest correct
  option: a `last_model`/`last_effort` pair on `processed_files`). This is the one place the
  claude-usage design needs genuine adaptation — claude-usage's records are self-contained,
  ours are stateful. Design it deliberately and test it explicitly.
- If mtime changed but file didn't grow: update mtime, skip.
- **Append-only CONFIRMED (Phase 0).** Resumed sessions append to the same file; they re-emit a
  fresh `session_meta` line (14 of 63 files carry 2–10 `session_meta` records) but the
  `payload.id` **never changes within a file** (0 mismatches observed), and `total_token_usage`
  is monotonic — it **never decreased** in any file, even the 3 files containing compaction
  records. Consequences the parser must honor:
  - **Multiple `session_meta` per file is normal** — do not treat a 2nd session_meta as a new
    session; key everything on `payload.id` and merge.
  - Each resume segment re-emits `turn_context` before its token_counts, so model carry-forward
    re-establishes naturally at resume boundaries — the incremental "new lines only" pass will
    see a fresh turn_context whenever a resume happened. The persisted carry-forward state
    (below) is still needed for the case where new lines begin *mid-segment* (a turn_context in
    a previously-read region, token_counts in the new region).
  - Defensive `lines`-shrank fallback (full reparse: delete session's turns, reinsert) is
    retained but is not an expected path given append-only behavior.

### 4.4 Reconciliation (correctness backstop)

After every scan that touched files, recompute `sessions` totals from `turns` (same closing
`UPDATE ... SELECT SUM` as claude-usage). Additionally — because token_count has no message id —
add a **consistency check**: for each touched session, compare summed `last_token_usage` deltas
against the final `total_token_usage` seen in the file; log a warning on drift > 1%. This is
diagnostic only in v1 (print, don't fail).

### 4.5 Session primary-model priority

Claude-usage ranks opus > sonnet > haiku so a subagent's cheap model doesn't overwrite the
session's real model. GPT equivalent:

```python
MODEL_PRIORITY = {"gpt-5.6": 6, "gpt-5.5": 5, "gpt-5.4": 4, "gpt-5.3": 3,
                  "gpt-5": 2, "codex-auto-review": 1}   # longest-prefix match wins
```

Per-turn model is always honored in `turns`; priority applies only to the session summary.
`codex-auto-review` (the built-in review model — heavily present in real data, 50 turn_contexts
observed locally) ranks lowest so it never masks the user's chosen model.

---

## 5. Pricing

Same three-tier resolution as claude-usage (`exact → startswith → substring-family fallback`),
same paired-dict convention (Python in `cli.py`, JS in `dashboard.py`, parity-tested).

**Cost formula (differs from Claude! — cached tokens are a subset of input):**

```python
cost = (input_tokens - cached_input_tokens) * p["input"]  / 1e6 \
     + cached_input_tokens                  * p["cached"] / 1e6 \
     + output_tokens                        * p["output"] / 1e6
# reasoning_output_tokens: already inside output_tokens — never priced separately
```

Seed table — **⚠ [VERIFY] a Phase-2 task must confirm current prices at
https://openai.com/api/pricing/ before release** (gpt-5.4/5.5/5.6 pricing is post-knowledge-
cutoff; the gpt-5 row is the only one known-good as a historical anchor: $1.25 / $0.125 / $10):

```python
PRICING = {   # $ per MTok: input, cached (cache-read), output
    "gpt-5.6":          {"input": ?, "cached": ?, "output": ?},   # verify
    "gpt-5.5":          {"input": ?, "cached": ?, "output": ?},   # verify
    "gpt-5.4":          {"input": ?, "cached": ?, "output": ?},   # verify
    "gpt-5.3-codex":    {"input": ?, "cached": ?, "output": ?},   # verify
    "gpt-5":            {"input": 1.25, "cached": 0.125, "output": 10.00},
    "codex-auto-review": None,  # bundled review model — bill at $0, show n/a  [VERIFY]
}
```

Unknown models → `None` → $0 / `n/a` (claude-usage's deliberate rule; keep it). README carries
the same caveat as claude-usage: these are API prices; Plus/Pro/Business subscription users have
a different real cost structure — which is exactly why the rate-limit charts matter more than
dollars for subscribers.

---

## 6. CLI (`cli.py`)

Mirror claude-usage command-for-command:

```
python cli.py scan [--sessions-dir PATH]
python cli.py today
python cli.py week
python cli.py stats
python cli.py dashboard [--sessions-dir PATH] [--host H] [--port P] [--no-browser]
python cli.py --version
```

Same output shapes as claude-usage (`today`: per-model table + totals + session/subagent/cache
lines; `week`: per-day + per-model; `stats`: all-time + by-model + top-5 projects + 30-day daily
average). GPT-specific additions:

- `today`/`stats` show **reasoning tokens** and **cached %** lines.
- `stats` shows a **thread-source split** (user / subagent / automation).
- `today` ends with the **latest rate-limit snapshot**: plan type, primary/secondary window %
  used and reset time — the "am I about to hit my weekly limit?" answer in the terminal.

Argument parsing stays hand-rolled (`parse_named_arg`) exactly like claude-usage — no argparse
needed at this size, and it keeps the file trivially readable.

`dashboard` binds the port first and scans in a background thread (claude-usage learned this
the hard way for cold scans; keep the pattern and the explanatory comment).

## 7. Dashboard (`dashboard.py`)

Single `HTML_TEMPLATE` string, Chart.js from CDN, 30s auto-refresh, two endpoints
(`GET /api/data` returns everything, client filters; `POST /api/rescan` drops DB + full rescan).
Model filter + date-range dropdown with URL params, collapsible sections in localStorage —
all straight ports of claude-usage behavior.

Charts, in section order:

1. **Headline tiles** — today's tokens, today's est. cost, sessions today, current primary
   rate-limit % with reset countdown.
2. **Rate-limit history** (GPT-exclusive) — line chart of primary & secondary `used_percent`
   over time from `rate_limit_snapshots`, with plan-type annotation.
3. **Daily tokens** — stacked bars: fresh input / cached input / output (reasoning shown as a
   line overlay or hover detail).
4. **Daily cost by model** — stacked bars.
5. **Model mix** — doughnut, tokens by model.
6. **Hourly heatmap or bars** — usage by hour of day.
7. **Projects table** — tokens/cost/sessions per project.
8. **Sessions table** — topic (from session_index), project, model, source badge
   (user/subagent/automation), tokens, est. cost.

## 8. Testing strategy

stdlib `unittest`, `tests/` directory, mirroring claude-usage's suite shape:

- **Golden fixtures**: `tests/fixtures/` with small synthetic rollout files hand-built to cover
  every quirk in §2.3 (old-format `info:null`, string-vs-object `source`, subagent thread,
  automation thread, mid-session model switch, missing turn_context). Fixtures are synthetic —
  **never commit real rollout content** (it contains conversation text and paths).
- Every test uses `tempfile` DBs; **no test may read the real `~/.codex` or write outside
  tmp** — enforce by always passing explicit paths.
- Must-have cases: last_token_usage-vs-total arithmetic; incremental scan of a grown file
  (including model carry-forward across the incremental boundary — the §4.3 subtlety);
  mtime-unchanged skip; reconciliation; pricing three-tier resolution + Python/JS parity
  (regex-extract the JS dict like claude-usage's test does); cost formula (cached subset!);
  `/api/data` smoke test; `--version`.

CI: copy claude-usage's `tests.yml` (unittest discover on 3.9/3.11/3.12) once the repo is on
GitHub.

---

## 9. Phased build plan (agent task breakdown)

Each phase is sized for one focused agent session, ends in a working state, and has explicit
acceptance criteria. Later phases must not begin until the prior phase's criteria pass.

### Phase 0 — Fixtures & format validation (foundation) — ✅ DONE

1. ✅ Read-only probe walked all 63 local rollout files. Census recorded in §2.4; the four open
   questions resolved: (a) 0 files have token_count without turn_context; (b) files are
   append-only, resumes re-emit same-id session_meta (§4.3); (c) `total_token_usage` never
   resets, even under compaction (§10); (d) full rate_limits/plan_type field census captured.
   All non-pricing **[VERIFY]** tags resolved in place. Remaining tags are Phase-2 pricing only.
2. ✅ Synthetic fixture set built (`tests/fixtures/`), covering every §2.3/§2.4 quirk.
3. ✅ Deliverable: updated plan + `tests/fixtures/*` + `tests/test_fixtures_sanity.py`.

**Accept:** all non-pricing [VERIFY] tags resolved; fixtures parse as valid JSONL and exercise
each documented quirk. **Met.**

### Phase 1 — `scanner.py`

1. Schema + `init_db` + migration helper + `VERSION = "0.1.0"`.
2. `parse_rollout_file`, `load_session_titles`, `scan()` with incremental logic and
   reconciliation.
3. `tests/test_scanner.py` covering the §8 must-haves.
4. Manual smoke: `python scanner.py` against real `~/.codex` completes without warnings and
   row counts look sane vs. the Phase-0 census.

**Accept:** full test pass; real-data smoke scan clean; second scan run skips all files and
finishes < 1s.

### Phase 2 — `cli.py`

1. Commands + pricing dict (**including the pricing verification task** — check
   openai.com/api/pricing, fill real numbers, document the "as of" date in README).
2. `tests/test_cli.py` (capture stdout, temp DB, seeded rows).

**Accept:** tests pass; `today`/`week`/`stats` output correct on real data; unknown models show
n/a and cost $0.

### Phase 3 — `dashboard.py` — ✅ DONE

1. Server + endpoints, ported structure from claude-usage's dashboard.py.
2. Embedded HTML with §7 charts; JS pricing parity test.
3. `tests/test_dashboard.py` (API shape, rescan contract with patched globals — preserve
   claude-usage's test-injection pattern of passing `db_path`/dirs explicitly).

**Accept:** tests pass; `python cli.py dashboard` opens a working dashboard on :8090 against
real data; both this and claude-usage's dashboard can run simultaneously. **Met.**

### Phase 3.5 — Accuracy & clarity fixes (from maintainer physical test)

Raised after a hands-on test of the shipped dashboard. Do these before Phase 4 ship-prep. Some
carry preliminary findings from a scoping pass (2026-07-15) — treat those as leads, verify before
implementing.

1. **Complete the model + pricing coverage.** Include every model OpenAI currently offers on
   consumer/business (ChatGPT/Codex) and API plans — GPT-5.4, 5.5, 5.6 and *all* subvariants
   (sol / terra / luna / mini / nano / pro), gpt-5.3-codex(-spark), and any image/other SKUs a
   Codex rollout could name. Reconcile the **two distinct OpenAI pricing surfaces** found while
   scoping:
   - **API pricing** (US$, `developers.openai.com/api/docs/pricing`) — what `cli.py`/`dashboard.py`
     currently use.
   - **Codex/ChatGPT-plan pricing** (**credits**, `learn.chatgpt.com/docs/pricing`) — e.g. GPT-5.6
     Sol = 125 / 12.5 / 750 credits per 1M (input/cached/output). Codex CLI users are on
     plan+credits+rate-limits, so the credit rate card may model their real spend better than API
     dollars.
   - **Decision to make & document:** price in US$ (API), show credits (plan), or offer both via a
     toggle. Keep the Python/JS pricing dicts in sync + parity-tested whichever way this lands.

2. **Resolve `codex-auto-review`.** Scoping found: it is Codex's *automatic code-review* pass
   (GitHub-integration "auto reviews"), it appears as a first-class `model` in `turn_context`
   (418 turns locally), and **OpenAI publishes no separate SKU** — official docs say auto-review
   "counts toward general Codex usage" (billed as normal Codex usage under an underlying GPT
   model). Third-party trackers assign it the gpt-5.4/terra tier (~$2.50 in / $15 out per 1M).
   → **Current behavior undercounts:** we bill it `None`→`n/a`→$0. Task: confirm the underlying
   model/tier from OpenAI docs, then either (a) price it at the terra/5.4 tier, or (b) keep n/a
   with a visible footnote — and document which, and why, in README + plan §5.

3. **Fix output-token visibility in the daily chart.** Output tokens **are** captured correctly
   (verified: 1.11M output all-time; 393K on 2026-07-15) — the bug is visual. Codex is extremely
   input-heavy: output is ~0.3% of input (~9% even of *fresh*, non-cached input), so on a linear
   stacked bar dominated by ~140M mostly-cached input the output slice is sub-pixel. Fix by
   plotting output on its own axis/series: e.g. a secondary y-axis line for output, a dedicated
   "output tokens/day" chart, or split input-vs-output into two panels. Add a short note that
   input ≫ output is expected for Codex (large context, small generations).

4. **Explain the Primary vs. Secondary rate-limit lines.** They are Codex's two rolling
   rate-limit windows (Phase 0 census: `primary.window_minutes` 10080 = weekly in recent data;
   older files also carry a shorter `secondary` window e.g. 300 min = 5h). Add an in-UI legend/
   tooltip/caption naming each window (derive the human label from `window_minutes`, as the CLI
   already does) and stating that % is share of that window's allowance consumed, with the reset
   time. Handle the null-secondary case gracefully.

5. **Add units to every chart axis.** Token charts → "tokens" (with K/M suffix), cost chart →
   "$" (or credits, per task 1), rate-limit chart → "% of window used", hourly → "turns". Label
   both axes where meaningful; keep tooltips unit-aware too.

6. **Scope a unified claude-usage + gpt-usage fork.** Investigate the cleanest way to observe
   both Claude and Codex/GPT usage from one dashboard. Deliverable is a written scoping doc (not
   an implementation), covering at least:
   - **Data-model reconciliation:** Anthropic (separate cache fields, message-id dedup) vs OpenAI
     (input-includes-cached, no message id, model on `turn_context`, rate-limit logging). A shared
     schema vs two schemas behind a common query layer.
   - **Provider abstraction:** one scanner dispatching to per-provider parsers; a `provider` column
     on turns/sessions; provider-aware pricing.
   - **Dashboard UX:** provider filter/toggle, unified vs side-by-side charts, cost normalization
     (Anthropic $ vs OpenAI $/credits).
   - **Fork logistics:** fork `phuryn/claude-usage` and add a Codex provider, vs keep two tools and
     add a combined view; upstreamability; how to keep the stdlib-only / three-file ethos (or
     whether a unified tool justifies relaxing it). Preserve MIT + attribution either way.

**Accept:** tasks 1–5 implemented, tested (pricing parity stays green), and eyeballed on real
data; task 6 delivered as a scoping doc under `docs/`. Model/pricing decisions recorded in
README + plan §5.

### Phase 3.6 — Dashboard parity with claude-usage v1.5.5

Scoped 2026-08-25 against `phuryn/claude-usage` v1.5.5 (confirmed current — `main` HEAD equals
the `v1.5.5` tag; the local reference clone at `../claude-usage-1.5.5` matches it exactly, so no
new upstream commits exist since gpt-usage's Phase 3 dashboard was built). All tasks below are
**pure `dashboard.py` (HTML/JS) changes** — every field they need already exists in `/api/data`
(`thread_source`, `topic`, `total_reasoning_tokens`, `total_cached_input`, `project_name`), so no
`scanner.py` schema change is required. Goal: match claude-usage's headline-metrics depth and
chart/table breadth using gpt-usage's own (OpenAI-shaped) data, not a literal port.

1. **Range-aware headline stats row.** Today's stats row (`renderStats()`) is hardcoded to
   *today only* and ignores the Range filter — selecting "Last 30 Days" doesn't change it, unlike
   claude-usage where the row recomputes for the selected range/model (`renderStats(totals)`,
   8 cards). Rebuild it to read from `filteredSessions()`/`filteredDaily()` (already
   range/model-filtered) and show: **Sessions, Turns, Input Tokens, Cached Input, Output Tokens,
   Reasoning Tokens, Subagent Tokens, Est. Cost** — plus keep the existing **Plan / rate-limit**
   tile (Codex-exclusive, claude-usage has no equivalent; don't drop it). Card labels get a
   `sub` line naming the active range, like claude-usage's `rangeLabel`.
2. **"Top Projects by Tokens" chart.** claude-usage has both a Projects *table* and a horizontal
   stacked-bar *chart* (top 10 projects, Input/Output series, `indexAxis:'y'`). gpt-usage only
   has the table. Add the chart (same shape, computed client-side from `filteredSessions()`
   grouped by project — no backend change) as a new chart-card above or beside the Projects
   table.
3. **"Tokens by Thread Source" chart.** claude-usage has a "Subagent Tokens by Type" horizontal
   stacked bar (Input/Output/Cache Read/Cache Creation per `agent_type`). gpt-usage has no
   per-agent-type table, but has the equivalent-purpose `thread_source` field (user / subagent /
   automation) already surfaced as a table column and color-coded (`src-user`/`src-subagent`/
   `src-automation`). Add a horizontal stacked bar chart — Fresh input / Cached input / Output
   per thread source — as gpt-usage's analog (three bars, not one per subagent type, since Codex
   doesn't sub-type its subagent threads the way Claude Code's `agent_type` does).
4. **"By Model" breakdown table.** claude-usage has both the Model Mix doughnut *and* a sortable
   `renderModelCostTable` (Model, Sessions, Turns, Input, Output, Cost). gpt-usage only has the
   doughnut. Add the table (Model, Sessions, Turns, Input, Cached, Output, Est. Cost — cached
   replaces claude-usage's cache-read/cache-write split, per gpt-usage's own cost model in §5),
   sortable like the existing Projects/Sessions tables.
5. **CSV export.** claude-usage exports every table (Sessions, Cost by Project, Cost by Model,
   Subagent Dispatches) via a per-table "⤓ CSV" button (`downloadCSV`, client-side `Blob`, no
   server round-trip). Add the same for gpt-usage's Sessions, Projects, and new By-Model tables
   — reuse claude-usage's pattern (a shared `downloadCSV(reportType, header, rows)` helper), not
   its exact column set (gpt-usage's rows differ per §5's cost model and lack of message ids).
6. **Calendar-aligned range presets.** claude-usage's Range selector has `today / week / month /
   prev-month / 7d / 30d / 90d / all`; gpt-usage only has `today / 7d / 30d / 90d / all` — no
   **This Week**, **This Month**, or **Previous Month** (calendar-boundary, not rolling-N-day).
   Add the three missing options to `rangeStart()`/`inRange()` and the `<select>`. Use local
   calendar-component boundaries (not `toISOString()`/UTC) per claude-usage's v1.5.5 fix (#151)
   for the exact bug it was patching — this is a chance to get it right the first time rather
   than reproduce and later re-fix it.
7. **Hourly chart: averaged, dual-metric.** claude-usage's hourly chart shows *average* turns/hour
   and *average* output-tokens/hour (bar + line, dual axis) over the N days in range, with a
   "N days averaged" caption and peak-hour bar highlighting — not a raw sum, which becomes
   misleading as the range grows (30d "hourly" totals dwarf 7d ones for reasons having nothing to
   do with usage pattern). gpt-usage's `renderHourly()` currently sums raw turns only. Switch to
   day-count-averaged turns (bar) + averaged output tokens (line, own axis), add the day-count
   caption, matching the clarity fix without needing claude-usage's TZ-toggle machinery (out of
   scope — see below).

**Explicitly out of scope (investigated, not carried over):**
- **Cost by Project & Branch table.** Requires a `git_branch` field claude-usage gets from
  Claude Code's transcripts. Codex rollout files carry no equivalent — the full verified field
  list in §2.2 (session_meta, turn_context) has no branch data, and the Phase-0 census (63 local
  files, 9 `cli_version`s) never observed one. Not buildable without new data Codex doesn't log;
  revisit only if a future Codex version starts emitting it.
- **Hourly chart TZ toggle** (local vs. UTC display). claude-usage added this because Claude Code
  users span many timezones with no single "right" default; gpt-usage's hourly chart already
  uses the browser's local offset, which is the more useful default for a single-machine local
  tool. Add only if a real user asks for UTC.
- **VS Code extension parity.** claude-usage ships one; gpt-usage has no VS Code extension in
  scope at all (not in §1 goals) — out of scope for a dashboard-parity pass.

**Accept:** all 7 tasks implemented and eyeballed against real local data; existing test suite
(`tests/test_dashboard.py`, pricing parity) stays green with no `scanner.py`/`/api/data` shape
changes required; the two explicitly-out-of-scope items documented above rather than silently
dropped.

### Phase 4 — Polish & release readiness

1. README finalization (screenshots, pricing table with date, "what is/isn't tracked").
2. CHANGELOG.md started at `## v0.1.0 — TBD` (adopt claude-usage's CHANGELOG-driven release
   conventions if/when a GitHub repo + workflows are added — not before).
3. Edge-case sweep: empty `~/.codex`, missing sessions dir, corrupt lines, huge files,
   Windows path handling (`cwd` values contain backslashes — `project_name_from_cwd` must
   normalize, claude-usage's version already does).
4. Optional stretch (only if all above is green): `pyproject.toml` for `uv tool install` /
   pipx parity with claude-usage (zero runtime deps preserved).

**Accept:** a new user can clone, run `python cli.py dashboard`, and get a correct dashboard
with no other steps, on Windows and POSIX.

### Standing rules for every agent working here

- Stdlib only. Adding a dependency fails review, no exceptions.
- Never write to `~/.codex` except `gpt-usage.db`; never commit real rollout content.
- Store token counts and metadata only — never conversation text.
- Keep the paired pricing dicts in sync and tested.
- Update this plan when reality diverges from it; the plan is living documentation.
- Follow `AGENTS.md` (repo conventions) — this file covers the *what*, AGENTS.md the *how*.

---

## 10. Risks and open questions

| Risk | Mitigation |
|---|---|
| Codex rollout schema keeps drifting (9 cli_versions in 5 months locally) | Defensive `.get()` everywhere; unknown types skipped; fixture per known variant; Phase-0 census re-runnable anytime |
| Resumed/forked sessions double-count if files are rewritten, not appended | Phase-0 verification; `lines`-shrank fallback (§4.3); reconciliation warning (§4.4) |
| ~~gpt-5.4+ pricing unknown at planning time~~ **(Phase 2: verified in US$)** | Prices filled from live API pricing (2026-07-15). **Open (Phase 3.5 task 1):** a second surface — Codex/ChatGPT *credit* pricing — may model Codex-plan users better than API $; reconcile the two |
| `codex-auto-review` billed as `n/a`/$0 undercounts cost | Phase 3.5 task 2: it's a real Codex usage pass (418 turns locally) with no separate SKU; decide terra/5.4-tier pricing vs footnoted n/a and document |
| Output tokens invisible in daily chart (input ≫ output for Codex) | Phase 3.5 task 3: data is correct (output ≈0.3% of input); plot output on its own axis/series |
| Rate-limit snapshot bloat | 15-min downsampling at insert (§4.1) |
| `session_index.jsonl` may not exist on all installs | Titles are optional decoration; sessions fall back to project name |
| ~~Compaction may reset `total_token_usage` mid-thread~~ **(Phase 0: not observed)** | Totals stayed monotonic in all 3 compaction files. We record `last_token_usage` per event regardless. Keep the diagnostic delta-clamp at ≥ 0 as cheap insurance against future drift |
| Resumed sessions re-emit `session_meta` (up to 10×/file) | Key on `payload.id`, merge — never treat a repeat session_meta as a new session (§4.3) |

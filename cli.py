"""
cli.py - Command-line interface for the Codex / GPT usage dashboard.

Commands:
  scan      - Scan rollout files and update the database
  today     - Print today's usage summary + current rate-limit status
  week      - Print the last 7 days (per-day + by-model)
  stats     - Print all-time usage statistics
  dashboard - Scan + open browser + start dashboard server (dashboard.py, Phase 3)

See docs/DEVELOPMENT_PLAN.md §5 (pricing) and §6 (CLI).
"""

import os
import sys
import sqlite3
from pathlib import Path
from datetime import date, datetime, timedelta

from scanner import VERSION

DB_PATH = Path(os.environ.get("GPT_USAGE_DB", Path.home() / ".codex" / "gpt-usage.db"))

# ── Pricing ─────────────────────────────────────────────────────────────────
# $ per 1M tokens: input (fresh), cached (cache-read), output. Reasoning tokens are a SUBSET
# of output and are never priced separately. input_tokens from the log INCLUDES cached, so
# calc_cost prices (input - cached) at the fresh rate and cached at the cached rate (§5).
#
# Prices verified against https://developers.openai.com/api/docs/pricing on 2026-08-25
# (re-checked after the 2026-08 GPT-5.6 price cut; gpt-5.6-sol/terra/luna dropped, everything
# gpt-5.4-and-earlier is unchanged since the 2026-07-15 check).
# This covers the full current GPT-5.4/5.5/5.6 roster and all subvariants (Phase 3.5 task 1).
# KEEP THIS PARITY with the PRICING const in dashboard.py — a test guards it.
#
# PRICING SURFACE (Phase 3.5 task 1 decision): we price in **US dollars using OpenAI API
# rates**. OpenAI also publishes a Codex/ChatGPT-plan *credit* rate card
# (https://learn.chatgpt.com/docs/pricing, e.g. GPT-5.6 Sol = 125/12.5/750 credits per 1M).
# Codex CLI users are on plan+credits+rate-limits, so a credit view may model their real spend
# better — that's tracked as a possible future toggle, but $ is the universally legible basis
# and the footer/README caveat that subscription costs differ. The rate-limit chart is the
# better signal for plan users regardless (see dashboard).
#
# Unlisted models resolve to None -> billed $0, shown as n/a (deliberate: never fabricate).
PRICING = {
    "gpt-5.6-sol":       {"input": 4.00, "cached": 0.40,  "output": 20.00},
    "gpt-5.6-terra":     {"input": 2.00, "cached": 0.20,  "output": 12.00},
    "gpt-5.6-luna":      {"input": 0.20, "cached": 0.02,  "output":  1.20},
    "gpt-5.5-pro":       {"input": 30.00, "cached": 3.00, "output": 180.00},
    "gpt-5.5":           {"input": 5.00, "cached": 0.50,  "output": 30.00},
    "gpt-5.4-mini":      {"input": 0.75, "cached": 0.075, "output":  4.50},
    "gpt-5.4-nano":      {"input": 0.20, "cached": 0.02,  "output":  1.25},
    "gpt-5.4-pro":       {"input": 30.00, "cached": 3.00, "output": 180.00},
    "gpt-5.4":           {"input": 2.50, "cached": 0.25,  "output": 15.00},
    "gpt-5.3-codex":     {"input": 1.75, "cached": 0.175, "output": 14.00},
    "gpt-5":             {"input": 1.25, "cached": 0.125, "output": 10.00},
    # codex-auto-review: Codex's automatic code-review pass (GitHub auto-reviews). OpenAI
    # publishes NO separate SKU — docs say it "counts toward general Codex usage" under an
    # underlying GPT model. Leaving it unpriced materially undercounts (418 turns locally), so
    # we ESTIMATE it at the gpt-5.4 tier that third-party trackers report ($2.50 / $0.25 / $15;
    # gpt-5.6-terra used to match this tier exactly but diverged after the 2026-08 price cut, so
    # this now anchors to gpt-5.4 specifically). Flagged as an estimate in the footer; revisit
    # if OpenAI names the underlying model (Phase 3.5 task 2).
    "codex-auto-review": {"input": 2.50, "cached": 0.25,  "output": 15.00},
}
# Models whose price is an estimate rather than a published SKU (for UI footnoting).
ESTIMATED_PRICING = {"codex-auto-review"}
# Longest-prefix first so gpt-5.4-mini wins over gpt-5.4, and gpt-5.6-sol over any gpt-5.6 stub.
_PRICING_KEYS = sorted(PRICING, key=len, reverse=True)


def get_pricing(model):
    if not model:
        return None
    if model in PRICING:
        return PRICING[model]
    for key in _PRICING_KEYS:
        if model.startswith(key):
            return PRICING[key]
    return None


def calc_cost(model, inp, cached, out):
    """Cost in $ for one model's tokens. inp INCLUDES cached (OpenAI semantics)."""
    p = get_pricing(model)
    if not p:
        return 0.0
    fresh = max(inp - cached, 0)
    return (fresh   * p["input"]  / 1_000_000 +
            cached  * p["cached"] / 1_000_000 +
            out     * p["output"] / 1_000_000)


# ── Formatting ──────────────────────────────────────────────────────────────

def fmt(n):
    n = n or 0
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(int(n))


def fmt_cost(c):
    return "n/a" if c is None else f"${c:.4f}"


def hr(char="-", width=64):
    print(char * width)


def require_db():
    if not DB_PATH.exists():
        print("Database not found. Run: python cli.py scan")
        sys.exit(1)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # Ensure schema is current before querying (mirrors claude-usage; cheap once migrated).
    from scanner import init_db
    init_db(conn)
    return conn


def _fmt_reset(epoch):
    if not epoch:
        return "?"
    try:
        return datetime.fromtimestamp(int(epoch)).strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError, OverflowError):
        return "?"


def _window_label(minutes):
    if not minutes:
        return "window"
    if minutes % 10080 == 0:
        return f"{minutes // 10080}w"
    if minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def _print_rate_limit(conn):
    row = conn.execute(
        "SELECT * FROM rate_limit_snapshots ORDER BY timestamp DESC LIMIT 1").fetchone()
    if not row:
        return
    plan = row["plan_type"] or "unknown"
    print(f"  Plan: {plan}")
    if row["primary_used_percent"] is not None:
        print(f"  Primary limit ({_window_label(row['primary_window_minutes'])}):"
              f"   {row['primary_used_percent']:.1f}% used, resets {_fmt_reset(row['primary_resets_at'])}")
    if row["secondary_used_percent"] is not None:
        print(f"  Secondary limit ({_window_label(row['secondary_window_minutes'])}):"
              f" {row['secondary_used_percent']:.1f}% used, resets {_fmt_reset(row['secondary_resets_at'])}")


# ── Shared queries ──────────────────────────────────────────────────────────

_BY_MODEL = """
    SELECT COALESCE(model, 'unknown') as model,
           SUM(input_tokens)            as inp,
           SUM(cached_input_tokens)     as cached,
           SUM(output_tokens)           as out,
           SUM(reasoning_output_tokens) as reasoning,
           COUNT(*)                     as turns,
           COUNT(DISTINCT session_id)   as sessions
    FROM turns {where}
    GROUP BY model
    ORDER BY inp + out DESC
"""


def _sum_row(rows):
    tot = {"inp": 0, "cached": 0, "out": 0, "reasoning": 0, "turns": 0, "cost": 0.0}
    for r in rows:
        tot["inp"]       += r["inp"] or 0
        tot["cached"]    += r["cached"] or 0
        tot["out"]       += r["out"] or 0
        tot["reasoning"] += r["reasoning"] or 0
        tot["turns"]     += r["turns"] or 0
        tot["cost"]      += calc_cost(r["model"], r["inp"] or 0, r["cached"] or 0, r["out"] or 0)
    return tot


def _print_model_rows(rows, indent="  "):
    for r in rows:
        cost = calc_cost(r["model"], r["inp"] or 0, r["cached"] or 0, r["out"] or 0)
        priced = get_pricing(r["model"]) is not None
        print(f"{indent}{r['model']:<20}  turns={fmt(r['turns']):<6}  "
              f"in={fmt(r['inp']):<8}  cached={fmt(r['cached']):<8}  out={fmt(r['out']):<8}  "
              f"cost={fmt_cost(cost) if priced else 'n/a':>10}")


# ── Commands ────────────────────────────────────────────────────────────────

def cmd_scan(sessions_dir=None):
    from scanner import scan
    scan(sessions_dir=sessions_dir)


def cmd_today():
    conn = require_db()
    today = date.today().isoformat()
    where = "WHERE substr(timestamp, 1, 10) = :d"
    rows = conn.execute(_BY_MODEL.format(where=where), {"d": today}).fetchall()

    print()
    hr()
    print(f"  Today's Usage  ({today})")
    hr()
    if not rows:
        print("  No usage recorded today.")
        print()
        _print_rate_limit(conn)
        print()
        conn.close()
        return

    _print_model_rows(rows)
    tot = _sum_row(rows)
    hr()
    print(f"  {'TOTAL':<20}  turns={fmt(tot['turns']):<6}  in={fmt(tot['inp']):<8}  "
          f"cached={fmt(tot['cached']):<8}  out={fmt(tot['out']):<8}  cost={fmt_cost(tot['cost']):>10}")

    sess = conn.execute(
        "SELECT COUNT(DISTINCT session_id) c FROM turns WHERE substr(timestamp,1,10)=?",
        (today,)).fetchone()
    sub = conn.execute(
        "SELECT COUNT(*) turns, SUM(input_tokens+output_tokens) tokens FROM turns "
        "WHERE substr(timestamp,1,10)=? AND COALESCE(is_subagent,0)=1", (today,)).fetchone()
    print()
    print(f"  Sessions today:    {sess['c']}")
    print(f"  Reasoning tokens:  {fmt(tot['reasoning'])}  (subset of output)")
    cached_pct = (100.0 * tot["cached"] / tot["inp"]) if tot["inp"] else 0.0
    print(f"  Cached input:      {fmt(tot['cached'])}  ({cached_pct:.0f}% of input)")
    print(f"  Subagent tokens:   {fmt(sub['tokens'] or 0)}  ({fmt(sub['turns'] or 0)} turns)")
    hr()
    _print_rate_limit(conn)
    hr()
    print()
    conn.close()


def cmd_week():
    conn = require_db()
    today_d = date.today()
    start_d = today_d - timedelta(days=6)
    start, end = start_d.isoformat(), today_d.isoformat()
    span = "WHERE substr(timestamp,1,10) BETWEEN :s AND :e"

    by_day_model = conn.execute("""
        SELECT substr(timestamp,1,10) as day, COALESCE(model,'unknown') as model,
               SUM(input_tokens) as inp, SUM(cached_input_tokens) as cached,
               SUM(output_tokens) as out, COUNT(*) as turns
        FROM turns WHERE substr(timestamp,1,10) BETWEEN ? AND ?
        GROUP BY day, model
    """, (start, end)).fetchall()
    by_model = conn.execute(_BY_MODEL.format(where=span), {"s": start, "e": end}).fetchall()
    sessions = conn.execute(
        "SELECT COUNT(DISTINCT session_id) c FROM turns WHERE substr(timestamp,1,10) BETWEEN ? AND ?",
        (start, end)).fetchone()

    print()
    hr()
    print(f"  Weekly Usage  ({start} to {end})")
    hr()
    if not by_model:
        print("  No usage recorded in the last 7 days.")
        print()
        conn.close()
        return

    per_day = {}
    for r in by_day_model:
        b = per_day.setdefault(r["day"], {"turns": 0, "inp": 0, "out": 0, "cost": 0.0})
        b["turns"] += r["turns"]
        b["inp"]   += r["inp"] or 0
        b["out"]   += r["out"] or 0
        b["cost"]  += calc_cost(r["model"], r["inp"] or 0, r["cached"] or 0, r["out"] or 0)

    print("  By Day:")
    for i in range(7):
        d = (start_d + timedelta(days=i)).isoformat()
        b = per_day.get(d, {"turns": 0, "inp": 0, "out": 0, "cost": 0.0})
        print(f"    {d}  turns={fmt(b['turns']):<5}  in={fmt(b['inp']):<8}  "
              f"out={fmt(b['out']):<8}  cost={fmt_cost(b['cost'])}")
    hr()
    print("  By Model:")
    _print_model_rows(by_model, indent="    ")
    tot = _sum_row(by_model)
    hr()
    print(f"    {'TOTAL':<20}  turns={fmt(tot['turns']):<6}  in={fmt(tot['inp']):<8}  "
          f"out={fmt(tot['out']):<8}  cost={fmt_cost(tot['cost'])}")
    print()
    print(f"  Sessions this week:  {sessions['c']}")
    print(f"  Reasoning tokens:    {fmt(tot['reasoning'])}")
    hr()
    print()
    conn.close()


def cmd_stats():
    conn = require_db()
    info = conn.execute(
        "SELECT COUNT(*) sessions, MIN(first_timestamp) first, MAX(last_timestamp) last "
        "FROM sessions").fetchone()
    by_model = conn.execute(_BY_MODEL.format(where="")).fetchall()
    tot = _sum_row(by_model)

    top_projects = conn.execute("""
        SELECT COALESCE(s.project_name,'unknown') as project, SUM(t.input_tokens) as inp,
               SUM(t.output_tokens) as out, COUNT(*) as turns,
               COUNT(DISTINCT t.session_id) as sessions
        FROM turns t LEFT JOIN sessions s ON t.session_id = s.session_id
        GROUP BY s.project_name ORDER BY inp + out DESC LIMIT 5
    """).fetchall()
    by_source = conn.execute("""
        SELECT COALESCE(s.thread_source,'user') as src, COUNT(*) as turns,
               SUM(t.input_tokens+t.output_tokens) as tokens
        FROM turns t LEFT JOIN sessions s ON t.session_id = s.session_id
        GROUP BY src ORDER BY tokens DESC
    """).fetchall()
    daily_avg = conn.execute("""
        SELECT AVG(di) ai, AVG(do_) ao FROM (
            SELECT substr(timestamp,1,10) d, SUM(input_tokens) di, SUM(output_tokens) do_
            FROM turns WHERE timestamp >= datetime('now','-30 days') GROUP BY d)
    """).fetchone()

    print()
    hr("=")
    print("  Codex / GPT Usage - All-Time Statistics")
    hr("=")
    print(f"  Period:           {(info['first'] or '')[:10]} to {(info['last'] or '')[:10]}")
    print(f"  Total sessions:   {info['sessions'] or 0:,}")
    print(f"  Total turns:      {fmt(tot['turns'])}")
    print()
    print(f"  Input tokens:     {fmt(tot['inp']):<12}  (includes cached)")
    print(f"  Cached input:     {fmt(tot['cached']):<12}  (cheaper cache reads)")
    print(f"  Output tokens:    {fmt(tot['out']):<12}  (includes reasoning)")
    print(f"  Reasoning tokens: {fmt(tot['reasoning']):<12}  (subset of output)")
    print()
    print(f"  Est. total cost:  ${tot['cost']:.4f}")
    hr()
    print("  By Model:")
    _print_model_rows(by_model)
    hr()
    print("  By Thread Source:")
    for r in by_source:
        print(f"    {r['src']:<12}  turns={fmt(r['turns']):<6}  tokens={fmt(r['tokens'])}")
    hr()
    print("  Top Projects:")
    for r in top_projects:
        print(f"    {r['project']:<40}  sessions={r['sessions']:<3}  "
              f"turns={fmt(r['turns']):<6}  tokens={fmt((r['inp'] or 0)+(r['out'] or 0))}")
    if daily_avg["ai"]:
        hr()
        print("  Daily Average (last 30 days):")
        print(f"    Input:   {fmt(int(daily_avg['ai'] or 0))}")
        print(f"    Output:  {fmt(int(daily_avg['ao'] or 0))}")
    hr("=")
    print()
    conn.close()


def cmd_dashboard(sessions_dir=None, host=None, port=None, no_browser=False):
    import threading
    import time
    from dashboard import serve  # Phase 3

    host = host or os.environ.get("HOST", "localhost")
    port = int(port or os.environ.get("PORT", "8090"))

    def background_scan():
        print("Scanning in the background...")
        cmd_scan(sessions_dir=sessions_dir)
        print("Background scan complete.")

    threading.Thread(target=background_scan, daemon=True).start()

    if not no_browser:
        import webbrowser

        def open_browser():
            time.sleep(1.0)
            webbrowser.open(f"http://{host}:{port}")

        threading.Thread(target=open_browser, daemon=True).start()

    serve(host=host, port=port)


# ── Entry point ─────────────────────────────────────────────────────────────

USAGE = """
Codex / GPT Usage Dashboard

Usage:
  python cli.py scan [--sessions-dir PATH]   Scan rollout files and update the database
  python cli.py today                        Show today's usage + rate-limit status
  python cli.py week                         Show last 7 days (per-day + by-model)
  python cli.py stats                        Show all-time statistics
  python cli.py dashboard [--sessions-dir PATH] [--host HOST] [--port PORT] [--no-browser]
                                             Scan + start dashboard (default http://localhost:8090)
  python cli.py --version                    Print the version and exit
"""

COMMANDS = {"scan": cmd_scan, "today": cmd_today, "week": cmd_week,
            "stats": cmd_stats, "dashboard": cmd_dashboard}


def parse_named_arg(args, flag):
    for i, a in enumerate(args):
        if a == flag and i + 1 < len(args):
            return args[i + 1]
    return None


def main():
    if len(sys.argv) >= 2 and sys.argv[1] in ("--version", "-V", "version"):
        print(VERSION)
        sys.exit(0)
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(USAGE)
        sys.exit(0)

    command = sys.argv[1]
    rest = sys.argv[2:]
    sessions_dir = parse_named_arg(rest, "--sessions-dir")

    if command == "dashboard":
        cmd_dashboard(sessions_dir=sessions_dir,
                      host=parse_named_arg(rest, "--host"),
                      port=parse_named_arg(rest, "--port"),
                      no_browser="--no-browser" in rest)
    elif command == "scan":
        cmd_scan(sessions_dir=sessions_dir)
    else:
        COMMANDS[command]()


if __name__ == "__main__":
    main()

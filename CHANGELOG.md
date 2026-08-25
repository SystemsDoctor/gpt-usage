# Changelog

## v0.1.0 — TBD

Initial build. Not yet released/tagged; tracked here as it stands on `main`.

### Scanner / CLI

- `scanner.py`: incremental scan of Codex's local JSONL rollout logs into a SQLite DB
  (`~/.codex/gpt-usage.db`), with per-turn model attribution (carried forward from
  `turn_context`), thread-source classification (user/subagent/automation/and others observed
  in the wild, e.g. `chatgpt_handoff`), rate-limit snapshot capture (15-min downsampled), and
  session-title backfill from `session_index.jsonl`.
- `cli.py`: `scan`, `today`, `week`, `stats`, `dashboard` commands, mirroring
  [claude-usage](https://github.com/phuryn/claude-usage)'s CLI shape. Cost estimates use
  OpenAI's published API pricing (see README's Pricing section and
  [docs/DEVELOPMENT_PLAN.md §5](docs/DEVELOPMENT_PLAN.md#5-pricing)); `codex-auto-review` is
  billed as an estimate (no published SKU) rather than silently at $0.

### Dashboard

- `dashboard.py`: single-file embedded HTML/JS dashboard (Chart.js from CDN, stdlib
  `http.server` otherwise) with headline stat tiles, rate-limit history, daily token/cost
  charts, model mix, top-projects and thread-source breakdowns, an hourly usage chart, and
  sortable Sessions/Projects/By-Model tables with CSV export.
- Range filter includes rolling windows (7/30/90 days, all time) and calendar-aligned presets
  (Today, This Week, This Month, Previous Month), plus a per-model filter.
- A US$ / Credits cost-unit toggle for Codex/ChatGPT-plan subscribers (credits = API US$ × 25).

### Project / docs

- `docs/DEVELOPMENT_PLAN.md`: the living architecture doc and phased build plan — the source of
  truth for the Codex rollout format, schema, pricing, and scan algorithm.
- `docs/UNIFIED_DASHBOARD_SCOPING.md`: scoping doc (not implemented) for a possible future
  dashboard unifying claude-usage and gpt-usage.
- stdlib `unittest` test suite (`tests/`) with synthetic fixtures covering the documented
  rollout-format quirks; no test touches a real `~/.codex` directory.

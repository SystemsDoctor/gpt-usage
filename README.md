# gpt-usage — Codex / GPT Usage Dashboard

**Status: in development — see [docs/DEVELOPMENT_PLAN.md](docs/DEVELOPMENT_PLAN.md) for the full architecture and build plan.**

A local usage dashboard for **Codex** (OpenAI's coding agent). Codex writes detailed session
logs locally — token counts, models, rate-limit consumption, projects — regardless of your plan.
This tool reads those logs and turns them into terminal summaries, charts, and cost estimates.

Sibling project to [claude-usage](https://github.com/phuryn/claude-usage), which does the same
for Claude Code. Same philosophy:

- **No API keys.** Everything is read from local files.
- **No install, no dependencies.** Python 3.8+ standard library only — no `pip install`, no
  venv, no build step.
- **Three Python files.** `scanner.py`, `cli.py`, `dashboard.py`. Run from any terminal.

## What this will track

Codex writes one JSONL "rollout" file per thread to `~/.codex/sessions/`. Captured surfaces:

- **Codex CLI** (`codex` in a terminal)
- **Codex VS Code extension**
- **Codex Desktop / Codex Work Desktop**
- **Subagent and automation threads** (attributed separately)

**Not captured:** ChatGPT web/desktop *chat* conversations — those are stored server-side and
leave no local token log. Only Codex surfaces write local rollout files.

**Privacy:** the tool stores token counts and metadata only — never conversation content — in a
local SQLite database (`~/.codex/gpt-usage.db`). Nothing ever leaves your machine.

## Planned usage

```
# Scan rollout files and populate the database
python cli.py scan

# Today's usage by model (+ current rate-limit status)
python cli.py today

# Last 7 days / all-time statistics
python cli.py week
python cli.py stats

# Scan + open the browser dashboard at http://localhost:8090
python cli.py dashboard
```

(Use `python3` on macOS/Linux.)

Unique to the GPT version: Codex logs your **plan type and rolling rate-limit consumption**
with every response, so the dashboard can chart how close you are to your weekly limit over
time — visibility the provider UI doesn't give you.

## Pricing

Costs shown in the CLI and dashboard are **estimates from OpenAI's published API pricing**
(verified 2026-08-25 against `developers.openai.com/api/docs/pricing`; see
[docs/DEVELOPMENT_PLAN.md §5](docs/DEVELOPMENT_PLAN.md#5-pricing) for the full table). Two
caveats:

- **Plus/Pro/Business plan users don't pay API rates.** Codex/ChatGPT plans meter usage in
  **credits**, at a flat 25× the API US$ rate for every model. The dashboard's header has a
  **US$ / Credits toggle** so subscribers can see the unit that matches their plan — the
  underlying data is always stored and computed in US$; the toggle only relabels the display.
  Either way, the rate-limit charts (your actual plan allowance) are the more reliable signal
  for subscribers than any dollar figure.
- **`codex-auto-review`** (Codex's automatic GitHub code-review pass) has no published SKU —
  OpenAI's docs say it counts toward general Codex usage under an unnamed underlying model. It's
  priced here as an **estimate** at the gpt-5.4/gpt-5.6-terra tier and marked with `*` wherever
  it appears, rather than silently billed at $0.

Unknown/future models price at $0 / `n/a` until added to the pricing table.

## Repository layout

| Path | Purpose |
|---|---|
| `docs/DEVELOPMENT_PLAN.md` | Architecture + phased build plan (the source of truth while building) |
| `docs/UNIFIED_DASHBOARD_SCOPING.md` | Scoping doc for a future combined Claude + Codex dashboard (not implemented) |
| `AGENTS.md` | Conventions for AI coding agents working on this repo |
| `CLAUDE.md` | Claude Code entry point (imports AGENTS.md) |
| `scanner.py` / `cli.py` / `dashboard.py` | The tool itself (built in Phases 1–3) |
| `tests/` | stdlib `unittest` suite with synthetic fixtures |

## Credits

The architecture, philosophy, and much of the design of this tool are directly modeled on
[claude-usage](https://github.com/phuryn/claude-usage) by **Pawel Huryn**
([@phuryn](https://github.com/phuryn)) of [The Product Compass Newsletter](https://www.productcompass.pm)
— a stdlib-only usage dashboard for Claude Code. gpt-usage is an independent sibling project
that applies the same approach to Codex's local session logs. Thank you, Pawel.

## License

[MIT](LICENSE).

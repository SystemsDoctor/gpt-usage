# gpt-usage — Codex / GPT Usage Dashboard

A local usage dashboard for **Codex** (OpenAI's coding agent). Codex writes detailed session
logs locally — token counts, models, rate-limit consumption, projects — regardless of your plan.
This tool reads those logs and turns them into terminal summaries, charts, and cost estimates.

Inspired by [claude-usage](https://github.com/phuryn/claude-usage), which does the same for
Claude Code — **gpt-usage is an independent project, not affiliated with or endorsed by
claude-usage or its author.** Same philosophy, though:

- **No API keys.** Everything is read from local files.
- **No install, no dependencies.** Python 3.8+ standard library only — no `pip install`, no
  venv, no build step.
- **Three Python files.** `scanner.py`, `cli.py`, `dashboard.py`. Run from any terminal.

## What this tracks

Codex writes one JSONL "rollout" file per thread to `~/.codex/sessions/`. Captured surfaces:

- **Codex CLI** (`codex` in a terminal)
- **Codex VS Code extension**
- **Codex Desktop / Codex Work Desktop**
- **Subagent and automation threads** (attributed separately)

**Not captured:** ChatGPT web/desktop *chat* conversations — those are stored server-side and
leave no local token log. Only Codex surfaces write local rollout files.

**Privacy:** the tool stores token counts and metadata only — never conversation content — in a
local SQLite database (`~/.codex/gpt-usage.db`). Nothing ever leaves your machine.

## Usage

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

### Quick-launch shortcut

`python cli.py dashboard` is the only command you'll run day to day. If you'd rather not retype
it, make yourself a shortcut — these aren't shipped in the repo (keeping it to three Python
files, per the project's own rule), just something you can create in a minute:

**Windows** — save this as `gpt-usage.bat` somewhere handy (e.g. your Desktop) and double-click
it, or pin it to the Start menu / taskbar:

```bat
@echo off
cd /d "C:\path\to\gpt-usage"
python cli.py dashboard
```

**macOS/Linux** — add an alias to your `~/.bashrc` / `~/.zshrc`:

```bash
alias gpt-usage='cd ~/path/to/gpt-usage && python3 cli.py dashboard'
```

or, on macOS, save the same two lines (with `#!/bin/bash` on top) as `gpt-usage.command`,
`chmod +x` it, and double-click it from Finder.

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
  priced here as an **estimate** at the gpt-5.4 tier and marked with `*` wherever it appears,
  rather than silently billed at $0.

Unknown/future models price at $0 / `n/a` until added to the pricing table.

## Repository layout

| Path | Purpose |
|---|---|
| `docs/DEVELOPMENT_PLAN.md` | Architecture + phased build plan (the source of truth while building) |
| `docs/UNIFIED_DASHBOARD_SCOPING.md` | Scoping doc for a future combined Claude + Codex dashboard (not implemented) |
| `AGENTS.md` | Conventions for AI coding agents working on this repo |
| `CLAUDE.md` | Claude Code entry point (imports AGENTS.md) |
| `scanner.py` / `cli.py` / `dashboard.py` | The tool itself |
| `tests/` | stdlib `unittest` suite with synthetic fixtures |
| `CHANGELOG.md` | Notable changes by version |

## Project status

This is a weekend project, maintained as time allows — there's no release schedule and no SLA.
Bug reports and feature requests are welcome as [GitHub Issues](../../issues), but there's no
guarantee any given one gets picked up promptly.

**This tool was built largely with AI assistance** (Claude and Codex, agentically, across most
of the design and code). It's provided as-is, for informational purposes only — review the code
yourself before trusting it with anything that matters, and don't take its cost estimates as
financial advice or a substitute for your actual OpenAI/Codex billing.

## Credits

The architecture, philosophy, and much of the design of this tool are directly modeled on
[claude-usage](https://github.com/phuryn/claude-usage) by **Pawel Huryn**
([@phuryn](https://github.com/phuryn)) of [The Product Compass Newsletter](https://www.productcompass.pm)
— a stdlib-only usage dashboard for Claude Code. gpt-usage applies the same approach to Codex's
local session logs; it is an independent project, not affiliated with or endorsed by
claude-usage or Pawel. Thank you, Pawel, for the inspiration.

## License

[MIT](LICENSE).

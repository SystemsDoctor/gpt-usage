# AGENTS.md

Guidance for any coding agent (Claude Code, Codex, etc.) working on this repository.

> **Naming note.** This project *analyzes* Codex's local usage logs, so "Codex" below usually
> refers to that product (the source of the JSONL rollout data) — not to the agent reading this
> file.

## What this project is

A local usage dashboard for Codex / GPT models, mirroring the architecture of
[claude-usage](https://github.com/phuryn/claude-usage) (reference clone at
`../claude-usage-1.5.5`). Three Python files, stdlib only, Python 3.8+, no install step.

**Read [docs/DEVELOPMENT_PLAN.md](docs/DEVELOPMENT_PLAN.md) before writing any code.** It is
the source of truth for the rollout-file format (verified against real local data), the SQLite
schema, the scan algorithm, the pricing rules, and the phased task breakdown with acceptance
criteria. This file covers repo conventions; the plan covers the design.

## Hard rules

1. **Standard library only.** Adding a third-party dependency fails review, no exceptions.
2. **Never write inside `~/.codex`** except the tool's own DB (`~/.codex/gpt-usage.db`).
   Treat everything else there as read-only foreign state.
3. **Never store or commit conversation content.** The DB holds token counts and metadata
   only. Test fixtures are synthetic — never copy real rollout lines into the repo.
4. **Tests never touch the real `~/.codex`** — use `tempfile` DBs and explicit fixture paths.
5. **Pricing lives in two places** (Python dict in `cli.py`, JS const in `dashboard.py`) and
   must stay in sync — keep the parity test green.
6. **Update the plan when reality diverges.** `docs/DEVELOPMENT_PLAN.md` is living
   documentation, not a historical artifact.

## Common commands

```
python cli.py scan                  # incremental scan (fast on re-run)
python cli.py today                 # today's usage by model + rate-limit status
python cli.py week                  # last 7 days
python cli.py stats                 # all-time stats
python cli.py dashboard             # scan + open http://localhost:8090

python -m unittest discover -s tests -v          # full test suite
python -m unittest tests.test_scanner -v         # one file
```

Use `python` on Windows, `python3` on macOS/Linux.

## Non-obvious invariants (will bite you)

These come from the verified rollout format — full detail in the plan, §2 and §4:

1. **`input_tokens` includes `cached_input_tokens`** (OpenAI semantics — opposite of
   Anthropic). Cost math must subtract cached from input before pricing fresh input.
2. **`reasoning_output_tokens` is a subset of `output_tokens`** — never add them together.
3. **Record one turn per `token_count` event using `last_token_usage`** (the per-response
   delta), never by differencing or summing `total_token_usage` cumulative values.
4. **The model is on `turn_context` records, not on usage records** — the parser carries the
   most recent model forward. Incremental scans must persist that carry-forward state across
   scan runs (see plan §4.3).
5. **Schema drifts across Codex versions**: `payload.source` is a string *or* an object,
   `token_count.info` can be `null`, `session_id` may be absent (use `payload.id`). Read
   everything with `.get()`, skip unknown record types silently.
6. **Session totals are recomputed from `turns` at the end of `scan()`** — preserve the
   reconciliation step if you refactor.

## Working style

- Match claude-usage's code style and structure where the designs align — it makes the two
  codebases mutually legible. Diverge only where the data model genuinely differs (and the
  plan documents every such divergence).
- Work in plan phases; don't start a phase before the previous one's acceptance criteria pass.
- Keep agent scratch work (probe scripts, notes, candidate lists) out of git — the
  `.gitignore` reserves `.agents/`, `notes/`, and similar paths for that. Committed files are
  deliverables only.

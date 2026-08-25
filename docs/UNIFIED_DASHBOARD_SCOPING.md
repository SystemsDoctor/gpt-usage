# Scoping: a unified claude-usage + gpt-usage dashboard

**Status:** scoping only — no implementation. Written to satisfy Phase 3.5 task 6 of
[DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md#phase-35--accuracy--clarity-fixes-from-maintainer-physical-test).
Grounded against the actual code of both tools: this repo (`gpt-usage`, Codex/OpenAI) and the
reference clone at `../claude-usage-1.5.5` ([phuryn/claude-usage](https://github.com/phuryn/claude-usage), Claude Code).

## 1. Why this is worth doing

Anyone running both Claude Code and Codex CLI currently has two dashboards, two databases, two
`localhost` ports, and no combined view of total AI-coding spend or rate-limit exposure across
providers. The tools are close enough in shape (stdlib-only, SQLite, JSONL-log scanner, same
`today`/`week`/`stats`/`dashboard` CLI surface) that a merge is plausible, but the underlying
data models diverge in ways that are easy to get wrong silently (see §2). This doc scopes the
work; it does not commit to doing it.

## 2. Data-model reconciliation

The two tools' `turns` tables encode genuinely different provider semantics, not just different
column names. A shared schema has to resolve each of these, not paper over them:

| Dimension | claude-usage (Anthropic) | gpt-usage (OpenAI/Codex) |
|---|---|---|
| Cache accounting | `cache_read_tokens` and `cache_creation_tokens` are **separate from** `input_tokens` (three independent counters) | `cached_input_tokens` is a **subset of** `input_tokens` — must be subtracted before pricing fresh input (AGENTS.md invariant 1) |
| Cache pricing | Two rates: `cache_read` (cheap) and `cache_write` (expensive, priced *above* fresh input) | One rate: `cached` (cache-read only; Codex rollouts carry no cache-write event) |
| Reasoning tokens | Not applicable | `reasoning_output_tokens` is a subset of `output_tokens` — never priced separately (AGENTS.md invariant 2) |
| Turn identity / dedup | `message_id` on each turn; used to de-duplicate re-emitted assistant messages across resumed transcripts | No message id in the log; one turn per `token_count` event using `last_token_usage`, the per-response delta (AGENTS.md invariant 3) |
| Model attribution | Model is on the same event as the token usage | Model lives on separate `turn_context` records; the scanner must carry the most recent model forward across events **and across incremental scan runs** (AGENTS.md invariant 4, plan §4.3) |
| Rate limits | Not tracked (Claude Code doesn't log rolling-window consumption) | `rate_limit_snapshots` table: primary/secondary rolling windows, plan type, reset time — Codex-exclusive data with no Anthropic equivalent |
| Thread provenance | `is_subagent` + `agent_id`; an `agents` table with per-agent-type rollups | `thread_source` (user/subagent/automation) + `parent_thread_id` + `originator`; no per-agent-type rollup table |
| Session/file mapping | One file can accumulate multiple sessions over time (compaction) | One rollout file maps to exactly one thread id; resumed sessions re-emit the same `session_meta` id (plan §4.3) |
| Schema drift handling | Additive `ALTER TABLE` migrations for a handful of added columns | Same pattern, plus defensive `.get()` everywhere because `payload.source` is string-or-object, `token_count.info` can be `null`, etc. (AGENTS.md invariant 5) |

**Recommendation: two schemas behind a common query layer, not one shared table.** Forcing
Anthropic's three-way cache split and Codex's subset-cache split into identical columns would
require either lossy conversion (drop cache-write pricing) or a column that's always null for
one provider (drop-cache-write-info for Codex rows, cache-write price for nothing). Instead:

- Keep `turns_claude` and `turns_codex` (or `claude_usage.db` / `gpt-usage.db` as two attached
  SQLite files) with each provider's native columns, preserving each tool's existing, tested
  scanner untouched.
- Add a thin **normalized view** per provider — `fresh_input`, `cached_input`, `output`,
  `cost_usd`, `model`, `provider`, `timestamp`, `session_id` — computed with each provider's own
  cost formula (AGENTS.md invariant 1 is exactly the divergence a shared raw table would hide).
  This view is what the dashboard's cross-provider charts query; native tables stay
  provider-specific for anything that needs the extra fields (rate limits, cache-write cost,
  agent rollups).
- A single `provider` column (`'claude'` | `'codex'`) tags every row in the normalized view.

## 3. Provider abstraction

- **Scanner dispatch.** One `scan()` entry point that reads a config of `{provider, sessions_dir,
  parser_fn}` and dispatches to `claude_usage.scanner.parse_rollout_file` or
  `gpt_usage.scanner.parse_rollout_file` per provider. Each parser keeps writing to its own
  tables unchanged (see §2) — the merge point is only the normalized view and the CLI/dashboard
  layer above it, not the parsers themselves. This bounds the blast radius: a schema-drift bug
  in one provider's parser can't corrupt the other's data.
- **Pricing.** Both tools already use the identical `exact → startswith → substring-family`
  three-tier resolver with a paired Python/JS dict (claude-usage's `PRICING` keys models like
  `claude-sonnet-4-5`; gpt-usage's keys models like `gpt-5.6-terra`). A unified tool would keep
  **two** pricing dicts (the cost *formula* differs per §2, not just the numbers) but expose one
  `get_pricing(provider, model)` facade. gpt-usage's `ESTIMATED_PRICING` marker (for
  `codex-auto-review`, plan §5) generalizes cleanly as a per-provider estimate-flag set.
  Anthropic has no current equivalent, but the mechanism is provider-agnostic.
- **CLI.** `today`/`week`/`stats` gain a `--provider claude|codex|all` filter (default `all`);
  output sections are already per-model tables, so the natural extension is a provider column or
  a "==== Claude ====" / "==== Codex ====" section header — mirroring how gpt-usage's `today`
  already appends a rate-limit section Claude's doesn't have. No output format needs to be
  invented from scratch.

## 4. Dashboard UX

- **Provider filter/toggle**, styled like gpt-usage's existing US$/credits toggle
  (`dashboard.py` `currency-select`, plan §5) — a header dropdown (`All` / `Claude` / `Codex`),
  URL-param-backed and `localStorage`-persisted like the existing model/date-range filters in
  both tools.
- **Unified vs. side-by-side charts:** default to **unified stacked-by-provider** for the charts
  that are provider-agnostic in meaning (daily tokens, daily cost, hourly heatmap — provider
  becomes another stack/series dimension, same pattern gpt-usage already uses for
  fresh-input/cached-input/output stacking). Keep provider-exclusive charts **side-by-side or
  provider-gated**: Codex's rate-limit history chart has no Claude analog and should just be
  hidden (not zero-filled) when `provider=claude`; if claude-usage ever adds a comparable
  metric, add a Claude-native panel next to it rather than forcing one chart to mean two things.
- **Cost normalization:** all costs render in US$ by default (both tools already price in US$
  natively); gpt-usage's existing credits toggle stays Codex-only since Claude has no published
  credit rate card. Do **not** attempt to normalize Claude $ and Codex credits into one number —
  keep them as clearly-labeled, separately-toggleable units, per the existing precedent in
  gpt-usage's own dashboard (plan §5's "toggle rather than convert" decision).

## 5. Fork logistics

Two real options, not a false binary:

**Option A — fork claude-usage, add a Codex provider.**
Pros: inherits claude-usage's maturity (CHANGELOG-driven releases, CI matrix, `pyproject.toml`
packaging, VS Code extension, Homebrew formula — none of which gpt-usage has built yet, see
Phase 4). Cons: gpt-usage's schema and scanner would need to be transplanted wholesale into a
codebase not designed for a second provider; every claude-usage upstream release would need
re-merging; attribution gets murkier (whose repo is it, who maintains it).

**Option B — keep two tools, add a combined view as a third artifact.**
A small `unified.py` (or a `combined-dashboard/` mini-tool) that reads both existing SQLite DBs
read-only and serves the merged dashboard from §4, with zero changes required to either
`claude-usage` or `gpt-usage` internals. Pros: both tools stay independently releasable and
upstreamable; no fork-divergence risk; matches this project's existing "sibling project" framing
(README.md's "Credits" section) rather than requiring a rename or re-attribution. Cons: some
duplicated boilerplate (HTTP server, HTML shell) between three files instead of two; users who
want *only* the unified view still need both source DBs populated (i.e., both original tools
installed and scanned at least once).

**Recommendation: Option B.** It's reversible, requires no permission or coordination with
`phuryn/claude-usage` upstream, and is the smaller change — it can be built and thrown away
without disturbing either shipped tool. If a unified tool proves popular enough to want single
distribution, it can absorb both scanners into itself later (effectively becoming Option A from
a fresh repo) — going the other direction (un-forking) is much harder.

**Stdlib-only ethos:** both tools deliberately have zero runtime dependencies (AGENTS.md hard
rule 1; claude-usage's own AGENTS.md carries the equivalent rule). A unified tool should keep
this — the combined view is still "read two SQLite files, serve one HTML page," which stdlib
`http.server` + `sqlite3` handles today. Nothing in §2–§4 requires a dependency to implement.

**Licensing:** both tools are MIT. Option B requires no license changes — a new file crediting
both origins is sufficient. Option A (forking claude-usage) must preserve claude-usage's MIT
license and Pawel Huryn attribution in full, per the existing precedent in this repo's own
README Credits section.

## 6. Non-goals of this doc

This is a scoping doc, not a design doc or implementation plan. It intentionally does not
specify: exact table/column names for a unified schema, the `unified.py` file layout, a phased
build plan, or a decision on whether to actually build this. Those are follow-up work if and
when a maintainer decides to proceed — see Phase 3.5 task 6 acceptance criteria in
[DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md).

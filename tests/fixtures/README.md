# Test fixtures

**Synthetic** Codex rollout files. Every value here is fabricated to exercise a specific quirk
documented in `docs/DEVELOPMENT_PLAN.md` §2.3/§2.4. **No real conversation content, paths, or
token counts** are ever copied from live `~/.codex` data into this directory.

Token arithmetic in each fixture is internally consistent (`total_tokens` = `input_tokens` +
`output_tokens`; `total_token_usage` is monotonic and equals the running sum of
`last_token_usage`) so Phase 1 scanner tests can assert exact expected values.

| File | Quirks exercised |
|---|---|
| `user_basic.jsonl` | Normal user thread; string `source`; mid-session model switch (gpt-5.5 → gpt-5.6-sol); cached-subset-of-input; reasoning-subset-of-output; monotonic totals; `plan_type: "plus"` |
| `old_format.jsonl` | 0.98.0 era: `token_count.info == null`; `session_meta` has no `session_id` field (only `id`); `rate_limits.plan_type == null`, no `limit_id`/`limit_name` |
| `subagent.jsonl` | `thread_source: "subagent"`; `source` is an **object** `{"subagent": {...}}`; `parent_thread_id` set |
| `automation.jsonl` | `thread_source: "automation"` |
| `resume_multi_meta.jsonl` | Two `session_meta` records with the **same** `payload.id` (resume); model carry-forward across the resume boundary; monotonic totals across segments |
| `noise_records.jsonl` | Skip-list coverage: `world_state`, `thread_settings_applied`, `response_item`, `agent_reasoning`, `context_compacted`, `turn_aborted`, `thread_name_updated`; one **malformed JSON** line; valid token_counts interleaved; totals monotonic across a compaction event |

The tiny `base_instructions` blobs are placeholders — real ones are ~10 KB and are always
ignored by the scanner.

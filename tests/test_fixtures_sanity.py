"""Phase 0 fixture sanity tests.

Validates the synthetic rollout fixtures in tests/fixtures/ *before* scanner.py exists.
Each fixture must be valid JSONL (one intentional malformed line excepted) and must actually
exercise the quirk it claims to (see tests/fixtures/README.md and DEVELOPMENT_PLAN.md §2.3/§2.4).

These assertions double as the executable spec Phase 1's scanner is built against — the token
arithmetic checked here is exactly what the scanner must reproduce.
"""
import json
import unittest
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    """Return (records, malformed_count) for a fixture, skipping blank/malformed lines."""
    records, malformed = [], 0
    for line in (FIXTURES / name).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            malformed += 1
    return records, malformed


def token_counts(records):
    """Every token_count payload with a non-null info block."""
    out = []
    for r in records:
        p = r.get("payload", {}) or {}
        if r.get("type") == "event_msg" and p.get("type") == "token_count" and p.get("info"):
            out.append(p["info"])
    return out


class TestAllFixtures(unittest.TestCase):
    NAMES = ["user_basic", "old_format", "subagent", "automation",
             "resume_multi_meta", "noise_records"]

    def test_all_present(self):
        for n in self.NAMES:
            self.assertTrue((FIXTURES / f"{n}.jsonl").exists(), f"missing fixture {n}")

    def test_first_record_is_session_meta(self):
        for n in self.NAMES:
            recs, _ = load(f"{n}.jsonl")
            self.assertEqual(recs[0]["type"], "session_meta", f"{n}: first line not session_meta")

    def test_token_arithmetic_internally_consistent(self):
        # total_tokens == input + output; reasoning <= output; cached <= input.
        # (OpenAI semantics: input already includes cached; reasoning is a subset of output.)
        for n in self.NAMES:
            recs, _ = load(f"{n}.jsonl")
            for info in token_counts(recs):
                for key in ("last_token_usage", "total_token_usage"):
                    u = info[key]
                    self.assertEqual(u["total_tokens"], u["input_tokens"] + u["output_tokens"],
                                     f"{n}: {key} total != input+output")
                    self.assertLessEqual(u["reasoning_output_tokens"], u["output_tokens"],
                                         f"{n}: reasoning exceeds output")
                    self.assertLessEqual(u["cached_input_tokens"], u["input_tokens"],
                                         f"{n}: cached exceeds input")

    def test_totals_monotonic(self):
        # total_token_usage.total_tokens never decreases within a file (Phase 0 finding).
        for n in self.NAMES:
            recs, _ = load(f"{n}.jsonl")
            last = -1
            for info in token_counts(recs):
                tot = info["total_token_usage"]["total_tokens"]
                self.assertGreaterEqual(tot, last, f"{n}: totals went backwards")
                last = tot


class TestUserBasic(unittest.TestCase):
    def setUp(self):
        self.recs, self.malformed = load("user_basic.jsonl")

    def test_no_malformed(self):
        self.assertEqual(self.malformed, 0)

    def test_source_is_string(self):
        self.assertIsInstance(self.recs[0]["payload"]["source"], str)

    def test_mid_session_model_switch(self):
        models = [r["payload"]["model"] for r in self.recs if r["type"] == "turn_context"]
        self.assertEqual(models, ["gpt-5.5", "gpt-5.6-sol"])

    def test_running_sum_equals_totals(self):
        # Sum of last_token_usage deltas must equal the final total_token_usage.
        infos = token_counts(self.recs)
        run = sum(i["last_token_usage"]["total_tokens"] for i in infos)
        self.assertEqual(run, infos[-1]["total_token_usage"]["total_tokens"])
        self.assertEqual(run, 3380)


class TestOldFormat(unittest.TestCase):
    def setUp(self):
        self.recs, _ = load("old_format.jsonl")

    def test_has_info_null_token_count(self):
        nulls = [r for r in self.recs
                 if r.get("type") == "event_msg"
                 and (r["payload"].get("type") == "token_count")
                 and r["payload"].get("info") is None]
        self.assertEqual(len(nulls), 1)

    def test_session_meta_lacks_session_id(self):
        self.assertNotIn("session_id", self.recs[0]["payload"])
        self.assertIn("id", self.recs[0]["payload"])

    def test_plan_type_null_and_no_limit_id(self):
        tc = next(r["payload"] for r in self.recs
                  if r.get("type") == "event_msg" and r["payload"].get("type") == "token_count")
        self.assertIsNone(tc["rate_limits"]["plan_type"])
        self.assertNotIn("limit_id", tc["rate_limits"])


class TestSubagent(unittest.TestCase):
    def setUp(self):
        self.meta = load("subagent.jsonl")[0][0]["payload"]

    def test_source_is_object(self):
        self.assertIsInstance(self.meta["source"], dict)
        self.assertIn("subagent", self.meta["source"])

    def test_thread_source_and_parent(self):
        self.assertEqual(self.meta["thread_source"], "subagent")
        self.assertEqual(self.meta["parent_thread_id"], "sess-user-0001")


class TestAutomation(unittest.TestCase):
    def test_thread_source_automation(self):
        meta = load("automation.jsonl")[0][0]["payload"]
        self.assertEqual(meta["thread_source"], "automation")


class TestResumeMultiMeta(unittest.TestCase):
    def setUp(self):
        self.recs, _ = load("resume_multi_meta.jsonl")

    def test_two_session_metas_same_id(self):
        metas = [r for r in self.recs if r["type"] == "session_meta"]
        self.assertEqual(len(metas), 2)
        ids = {m["payload"]["id"] for m in metas}
        self.assertEqual(ids, {"sess-resume-0005"})


class TestNoiseRecords(unittest.TestCase):
    def setUp(self):
        self.recs, self.malformed = load("noise_records.jsonl")

    def test_exactly_one_malformed_line(self):
        self.assertEqual(self.malformed, 1)

    def test_skip_list_types_present(self):
        top = {r["type"] for r in self.recs}
        self.assertIn("world_state", top)
        self.assertIn("response_item", top)
        payload_types = {r["payload"].get("type") for r in self.recs
                         if r["type"] == "event_msg"}
        for t in ("thread_settings_applied", "agent_reasoning",
                  "context_compacted", "turn_aborted", "thread_name_updated"):
            self.assertIn(t, payload_types, f"missing skip-list type {t}")

    def test_two_valid_token_counts(self):
        self.assertEqual(len(token_counts(self.recs)), 2)


if __name__ == "__main__":
    unittest.main()

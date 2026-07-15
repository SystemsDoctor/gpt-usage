"""Phase 2 cli tests. Seeded temp DBs + stdout capture — never the real ~/.codex."""
import io
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timezone
from pathlib import Path

import cli
import scanner


def seed_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    scanner.init_db(conn)
    today = date.today().isoformat()
    conn.execute("INSERT INTO sessions (session_id, project_name, thread_source, "
                 "first_timestamp, last_timestamp, model) VALUES "
                 "('s1','acme/web','user',?,?,'gpt-5.5')", (today + "T09:00:00Z", today + "T10:00:00Z"))
    conn.execute("INSERT INTO sessions (session_id, project_name, thread_source, "
                 "first_timestamp, last_timestamp, model) VALUES "
                 "('s2','acme/web','subagent',?,?,'gpt-5.4')", (today + "T09:30:00Z", today + "T09:40:00Z"))
    turns = [
        ("s1", today + "T09:00:00Z", "gpt-5.5", 1000, 400, 100, 20, 0),
        ("s1", today + "T09:05:00Z", "gpt-5.6-sol", 500, 100, 80, 10, 0),
        ("s2", today + "T09:30:00Z", "gpt-5.4", 300, 0, 40, 5, 1),
        ("s1", today + "T09:40:00Z", "codex-auto-review", 200, 0, 15, 0, 0),
    ]
    conn.executemany(
        "INSERT INTO turns (session_id, timestamp, model, input_tokens, cached_input_tokens, "
        "output_tokens, reasoning_output_tokens, is_subagent) VALUES (?,?,?,?,?,?,?,?)", turns)
    conn.execute("UPDATE sessions SET total_input_tokens=(SELECT SUM(input_tokens) FROM turns "
                 "WHERE turns.session_id=sessions.session_id)")
    conn.execute("INSERT INTO rate_limit_snapshots (bucket, timestamp, plan_type, "
                 "primary_used_percent, primary_window_minutes, primary_resets_at) "
                 "VALUES ('b1', ?, 'plus', 7.5, 10080, 1784732025)", (today + "T09:40:00Z",))
    conn.commit()
    conn.close()
    return path


class TestPricing(unittest.TestCase):
    def test_exact_and_longest_prefix(self):
        self.assertEqual(cli.get_pricing("gpt-5.6-sol")["output"], 30.00)
        self.assertEqual(cli.get_pricing("gpt-5.6-terra")["output"], 15.00)
        # longest-prefix: mini must not be swallowed by the gpt-5.4 base row
        self.assertEqual(cli.get_pricing("gpt-5.4-mini")["input"], 0.75)
        self.assertEqual(cli.get_pricing("gpt-5.4")["input"], 2.50)
        # date-suffixed id resolves via startswith to the base tier
        self.assertEqual(cli.get_pricing("gpt-5.5-20260101")["input"], 5.00)

    def test_unknown_and_review_are_none(self):
        self.assertIsNone(cli.get_pricing("codex-auto-review"))
        self.assertIsNone(cli.get_pricing("mystery-model"))
        self.assertIsNone(cli.get_pricing(""))

    def test_cost_formula_cached_is_subset(self):
        # gpt-5.5: input 1000 (400 cached), output 100.
        # fresh=600 -> 600*5 + 400*0.5 + 100*30, all /1e6 = 0.0062
        self.assertAlmostEqual(cli.calc_cost("gpt-5.5", 1000, 400, 100), 0.0062, places=6)

    def test_cost_zero_for_unpriced(self):
        self.assertEqual(cli.calc_cost("codex-auto-review", 1000, 0, 100), 0.0)
        self.assertEqual(cli.calc_cost("mystery", 1000, 0, 100), 0.0)

    def test_reasoning_not_priced_separately(self):
        # reasoning is inside output; cost depends only on output total, not reasoning.
        self.assertEqual(cli.calc_cost("gpt-5.5", 500, 0, 200),
                         cli.calc_cost("gpt-5.5", 500, 0, 200))


class TestCommands(unittest.TestCase):
    def setUp(self):
        self.db = seed_db()
        self._orig = cli.DB_PATH
        cli.DB_PATH = Path(self.db)

    def tearDown(self):
        cli.DB_PATH = self._orig
        if os.path.exists(self.db):
            os.unlink(self.db)

    def _run(self, fn):
        buf = io.StringIO()
        with redirect_stdout(buf):
            fn()
        return buf.getvalue()

    def test_today_shows_models_totals_and_rate_limit(self):
        out = self._run(cli.cmd_today)
        self.assertIn("Today's Usage", out)
        self.assertIn("gpt-5.5", out)
        self.assertIn("gpt-5.6-sol", out)
        self.assertIn("TOTAL", out)
        self.assertIn("Plan: plus", out)
        self.assertIn("7.5% used", out)
        self.assertIn("Reasoning tokens", out)

    def test_today_unpriced_model_shows_na(self):
        out = self._run(cli.cmd_today)
        # codex-auto-review row must render n/a, not $0.0000
        line = [l for l in out.splitlines() if "codex-auto-review" in l][0]
        self.assertIn("n/a", line)

    def test_stats_thread_source_split(self):
        out = self._run(cli.cmd_stats)
        self.assertIn("By Thread Source", out)
        self.assertIn("subagent", out)
        self.assertIn("Top Projects", out)
        self.assertIn("acme/web", out)

    def test_week_runs(self):
        out = self._run(cli.cmd_week)
        self.assertIn("Weekly Usage", out)
        self.assertIn("By Model", out)

    def test_missing_db_exits(self):
        cli.DB_PATH = Path(self.db + ".nope")
        with self.assertRaises(SystemExit):
            self._run(cli.cmd_today)


if __name__ == "__main__":
    unittest.main()

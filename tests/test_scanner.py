"""Phase 1 scanner tests. Temp DBs + explicit fixture paths only — never the real ~/.codex."""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

import scanner

FIXTURES = Path(__file__).parent / "fixtures"


def fresh_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)  # let sqlite create it fresh
    return path


def query(db, sql, args=()):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


class TestHelpers(unittest.TestCase):
    def test_project_name_windows_path(self):
        self.assertEqual(
            scanner.project_name_from_cwd("C:\\Users\\dev\\proj\\alpha-svc"),
            "proj/alpha-svc")

    def test_project_name_empty(self):
        self.assertEqual(scanner.project_name_from_cwd(""), "unknown")

    def test_model_priority_longest_prefix(self):
        self.assertGreater(scanner._model_priority("gpt-5.6-sol"),
                           scanner._model_priority("gpt-5.5"))
        self.assertGreater(scanner._model_priority("gpt-5.3-codex"),
                           scanner._model_priority("gpt-5"))
        # bundled review model ranks lowest, unknown -> 0
        self.assertEqual(scanner._model_priority("gpt-5.5"),
                         5)
        self.assertGreater(scanner._model_priority("codex-auto-review"), 0)
        self.assertEqual(scanner._model_priority("mystery-model"), 0)

    def test_rate_bucket_15min(self):
        self.assertEqual(scanner._rate_bucket("2026-07-15T15:01:11.003Z"), "2026-07-15T15:0")
        self.assertEqual(scanner._rate_bucket("2026-07-15T15:16:00.000Z"), "2026-07-15T15:1")
        self.assertEqual(scanner._rate_bucket("2026-07-15T15:46:00.000Z"), "2026-07-15T15:3")


class TestScanFixtures(unittest.TestCase):
    """Scan the whole fixtures dir and assert cross-cutting facts."""

    @classmethod
    def setUpClass(cls):
        cls.db = fresh_db()
        cls.result = scanner.scan(sessions_dir=str(FIXTURES), db_path=cls.db,
                                   index_path=FIXTURES / "no_such_index.jsonl", verbose=False)

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(cls.db):
            os.unlink(cls.db)

    def test_sessions_created(self):
        rows = query(self.db, "SELECT session_id FROM sessions ORDER BY session_id")
        ids = {r["session_id"] for r in rows}
        self.assertEqual(ids, {
            "sess-user-0001", "sess-old-0002", "sess-sub-0003",
            "sess-auto-0004", "sess-resume-0005", "sess-noise-0006"})

    def test_user_basic_token_totals(self):
        # Sum of last_token_usage across the 3 events (plan-verified arithmetic).
        r = query(self.db, "SELECT * FROM sessions WHERE session_id = 'sess-user-0001'")[0]
        self.assertEqual(r["total_input_tokens"], 3000)     # 1000+1500+500
        self.assertEqual(r["total_output_tokens"], 380)     # 100+200+80
        self.assertEqual(r["total_cached_input"], 1400)     # 400+900+100
        self.assertEqual(r["total_reasoning_tokens"], 80)   # 20+50+10
        self.assertEqual(r["turn_count"], 3)

    def test_user_basic_primary_model_priority(self):
        # gpt-5.6-sol (last turn) outranks gpt-5.5 -> session model is the higher one.
        r = query(self.db, "SELECT model FROM sessions WHERE session_id = 'sess-user-0001'")[0]
        self.assertEqual(r["model"], "gpt-5.6-sol")

    def test_per_turn_model_attribution(self):
        models = [r["model"] for r in query(
            self.db, "SELECT model FROM turns WHERE session_id='sess-user-0001' ORDER BY id")]
        self.assertEqual(models, ["gpt-5.5", "gpt-5.5", "gpt-5.6-sol"])

    def test_info_null_skipped_but_second_turn_recorded(self):
        # old_format: first token_count has info:null (no turn), second is real.
        rows = query(self.db, "SELECT * FROM turns WHERE session_id='sess-old-0002'")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["input_tokens"], 800)
        self.assertEqual(rows[0]["model"], "gpt-5.3-codex")

    def test_subagent_flagged(self):
        r = query(self.db, "SELECT is_subagent FROM turns WHERE session_id='sess-sub-0003'")
        self.assertEqual(r[0]["is_subagent"], 1)
        s = query(self.db, "SELECT thread_source, parent_thread_id FROM sessions WHERE session_id='sess-sub-0003'")[0]
        self.assertEqual(s["thread_source"], "subagent")
        self.assertEqual(s["parent_thread_id"], "sess-user-0001")

    def test_automation_flagged(self):
        r = query(self.db, "SELECT is_subagent FROM turns WHERE session_id='sess-auto-0004'")
        self.assertEqual(r[0]["is_subagent"], 1)  # non-user thread_source

    def test_resume_single_session_row(self):
        # Two same-id session_meta -> exactly one session row, both turns attributed.
        rows = query(self.db, "SELECT * FROM sessions WHERE session_id='sess-resume-0005'")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["turn_count"], 2)
        self.assertEqual(rows[0]["total_input_tokens"], 1800)  # 1000 + 800

    def test_noise_records_only_two_turns(self):
        rows = query(self.db, "SELECT * FROM turns WHERE session_id='sess-noise-0006'")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["model"], "gpt-5.6-terra")

    def test_rate_snapshots_downsampled(self):
        # user_basic's 3 events are all within one 15-min slot -> 1 snapshot for that bucket.
        snaps = query(self.db, "SELECT * FROM rate_limit_snapshots")
        self.assertGreaterEqual(len(snaps), 1)
        buckets = [s["bucket"] for s in snaps]
        self.assertEqual(len(buckets), len(set(buckets)))  # unique per bucket
        plus = [s for s in snaps if s["plan_type"] == "plus"]
        self.assertTrue(plus)


class TestIncrementalScan(unittest.TestCase):
    def setUp(self):
        self.db = fresh_db()
        self.tmp = tempfile.mkdtemp()
        self.roll = Path(self.tmp) / "rollout-test.jsonl"

    def tearDown(self):
        if os.path.exists(self.db):
            os.unlink(self.db)

    def _write(self, lines, mode="w"):
        with open(self.roll, mode, encoding="utf-8") as f:
            for ln in lines:
                f.write(ln + "\n")

    SM = '{"timestamp":"2026-07-15T10:00:00.000Z","type":"session_meta","payload":{"id":"S","cwd":"C:\\\\a\\\\b","thread_source":"user","cli_version":"0.144.2","source":"vscode"}}'
    TC = '{"timestamp":"2026-07-15T10:00:01.000Z","type":"turn_context","payload":{"model":"gpt-5.5","effort":"medium"}}'

    def _tok(self, ts, inp, out, tot_in, tot_out):
        return ('{"timestamp":"%s","type":"event_msg","payload":{"type":"token_count",'
                '"info":{"total_token_usage":{"input_tokens":%d,"cached_input_tokens":0,'
                '"output_tokens":%d,"reasoning_output_tokens":0,"total_tokens":%d},'
                '"last_token_usage":{"input_tokens":%d,"cached_input_tokens":0,'
                '"output_tokens":%d,"reasoning_output_tokens":0,"total_tokens":%d}}}}'
                % (ts, tot_in, tot_out, tot_in + tot_out, inp, out, inp + out))

    def test_grow_and_carry_forward_model(self):
        # Segment 1: meta + turn_context(gpt-5.5) + one token_count.
        self._write([self.SM, self.TC, self._tok("2026-07-15T10:00:02.000Z", 100, 10, 100, 10)])
        scanner.scan(sessions_dir=self.tmp, db_path=self.db,
                     index_path=Path(self.tmp) / "none.jsonl", verbose=False)
        self.assertEqual(len(query(self.db, "SELECT * FROM turns")), 1)

        # Append a second token_count with NO new turn_context. Model must carry forward
        # from processed_files.last_model across the incremental boundary (plan §4.3).
        self._write([self._tok("2026-07-15T10:05:00.000Z", 200, 20, 300, 30)], mode="a")
        # bump mtime so the incremental path triggers
        os.utime(self.roll, (os.path.getmtime(self.roll) + 5, os.path.getmtime(self.roll) + 5))
        scanner.scan(sessions_dir=self.tmp, db_path=self.db,
                     index_path=Path(self.tmp) / "none.jsonl", verbose=False)

        turns = query(self.db, "SELECT model, input_tokens FROM turns ORDER BY id")
        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[1]["input_tokens"], 200)
        self.assertEqual(turns[1]["model"], "gpt-5.5")  # carried forward
        sess = query(self.db, "SELECT total_input_tokens, turn_count FROM sessions")[0]
        self.assertEqual(sess["total_input_tokens"], 300)
        self.assertEqual(sess["turn_count"], 2)

    def test_unchanged_file_skipped(self):
        self._write([self.SM, self.TC, self._tok("2026-07-15T10:00:02.000Z", 100, 10, 100, 10)])
        scanner.scan(sessions_dir=self.tmp, db_path=self.db,
                     index_path=Path(self.tmp) / "none.jsonl", verbose=False)
        r2 = scanner.scan(sessions_dir=self.tmp, db_path=self.db,
                          index_path=Path(self.tmp) / "none.jsonl", verbose=False)
        self.assertEqual(r2["skipped"], 1)
        self.assertEqual(r2["new"], 0)
        self.assertEqual(r2["updated"], 0)
        # no duplicate turns
        self.assertEqual(len(query(self.db, "SELECT * FROM turns")), 1)


class TestEmptyAndMissing(unittest.TestCase):
    def test_missing_sessions_dir(self):
        db = fresh_db()
        try:
            r = scanner.scan(sessions_dir="C:/no/such/dir/xyz", db_path=db, verbose=False)
            self.assertEqual(r["turns"], 0)
            self.assertEqual(r["sessions"], 0)
        finally:
            if os.path.exists(db):
                os.unlink(db)


class TestCorruptAndSparseFiles(unittest.TestCase):
    """Edge-case sweep (Phase 4): zero-byte/whitespace/binary-garbage rollout files, and a
    session_meta-only file (thread created, zero turns) must never crash a scan."""

    def _scan_dir(self, files):
        tmp = tempfile.mkdtemp()
        d = Path(tmp) / "2026" / "01" / "01"
        d.mkdir(parents=True)
        for name, content in files.items():
            (d / name).write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
        db = fresh_db()
        try:
            return scanner.scan(sessions_dir=tmp, db_path=db, verbose=False), db
        finally:
            pass  # caller unlinks db

    def test_zero_byte_whitespace_and_garbage_dont_crash(self):
        r, db = self._scan_dir({
            "rollout-empty.jsonl": "",
            "rollout-whitespace.jsonl": "   \n\n  \n",
            "rollout-garbage.jsonl": b"this is not json at all\n\x00\x01binary junk\xff\xfe\n",
        })
        try:
            self.assertEqual(r["turns"], 0)
            self.assertEqual(r["sessions"], 0)
        finally:
            if os.path.exists(db):
                os.unlink(db)

    def test_meta_only_session_still_creates_a_row(self):
        line = ('{"timestamp":"2026-01-01T00:00:00.000Z","type":"session_meta",'
                '"payload":{"id":"s-edge-1","cwd":"C:/Users/dev/proj/x","thread_source":"user"}}\n')
        r, db = self._scan_dir({"rollout-metaonly.jsonl": line})
        try:
            self.assertEqual(r["sessions"], 1)
            row = query(db, "SELECT project_name, thread_source FROM sessions "
                            "WHERE session_id='s-edge-1'")[0]
            self.assertEqual(row["project_name"], "proj/x")
            self.assertEqual(row["thread_source"], "user")
        finally:
            if os.path.exists(db):
                os.unlink(db)


class TestSessionTitles(unittest.TestCase):
    def test_titles_loaded_and_missing_ok(self):
        self.assertEqual(scanner.load_session_titles("C:/no/such/index.jsonl"), {})
        tmp = tempfile.mkdtemp()
        idx = Path(tmp) / "session_index.jsonl"
        idx.write_text(
            '{"id":"sess-user-0001","thread_name":"My Topic","updated_at":"2026-07-15T00:00:00Z"}\n'
            '{"bad json\n',
            encoding="utf-8")
        titles = scanner.load_session_titles(idx)
        self.assertEqual(titles.get("sess-user-0001"), "My Topic")

    def test_topic_applied_to_session(self):
        db = fresh_db()
        tmp = tempfile.mkdtemp()
        idx = Path(tmp) / "session_index.jsonl"
        idx.write_text('{"id":"sess-user-0001","thread_name":"Alpha Work"}\n', encoding="utf-8")
        try:
            scanner.scan(sessions_dir=str(FIXTURES), db_path=db, index_path=idx, verbose=False)
            r = query(db, "SELECT topic FROM sessions WHERE session_id='sess-user-0001'")[0]
            self.assertEqual(r["topic"], "Alpha Work")
        finally:
            if os.path.exists(db):
                os.unlink(db)


if __name__ == "__main__":
    unittest.main()

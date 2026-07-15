"""Phase 3 dashboard tests. Temp DBs + explicit fixture paths; never the real ~/.codex."""
import json
import os
import re
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import cli
import dashboard
import scanner

FIXTURES = Path(__file__).parent / "fixtures"


def seeded_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    scanner.scan(sessions_dir=str(FIXTURES), db_path=path,
                 index_path=FIXTURES / "none.jsonl", verbose=False)
    return path


class TestPricingParity(unittest.TestCase):
    """The JS PRICING const in dashboard.py must match cli.PRICING exactly (plan §5)."""

    def test_js_pricing_matches_python(self):
        m = re.search(r"const PRICING = (\{.*?\});", dashboard.HTML_TEMPLATE)
        self.assertIsNotNone(m, "could not locate JS PRICING object")
        js = json.loads(m.group(1))  # null -> None
        self.assertEqual(js, cli.PRICING)


class TestDashboardData(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = seeded_db()

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(cls.db):
            os.unlink(cls.db)

    def test_shape(self):
        data = dashboard.get_dashboard_data(Path(self.db))
        for key in ("all_models", "daily_by_model", "hourly", "sessions_all",
                    "rate_limits", "generated_at"):
            self.assertIn(key, data)
        self.assertTrue(data["all_models"])
        self.assertTrue(data["sessions_all"])

    def test_rate_limit_snapshots_present(self):
        data = dashboard.get_dashboard_data(Path(self.db))
        self.assertTrue(data["rate_limits"])
        r = data["rate_limits"][0]
        self.assertIn("primary_pct", r)
        self.assertIn("plan_type", r)

    def test_sessions_carry_source_and_topic_fields(self):
        data = dashboard.get_dashboard_data(Path(self.db))
        sub = [s for s in data["sessions_all"] if s["session_id"] == "sess-sub-0003"]
        self.assertEqual(len(sub), 1)
        self.assertEqual(sub[0]["source"], "subagent")

    def test_missing_db_returns_error(self):
        data = dashboard.get_dashboard_data(Path(self.db + ".nope"))
        self.assertIn("error", data)


class TestEndpoints(unittest.TestCase):
    """Live server round-trip honoring the patched-DB_PATH contract."""

    def setUp(self):
        self.db = seeded_db()
        self._orig_db = dashboard.DB_PATH
        self._orig_dir = scanner.SESSIONS_DIR
        dashboard.DB_PATH = Path(self.db)
        # Point rescan at fixtures, not the real ~/.codex.
        scanner.SESSIONS_DIR = FIXTURES
        self.server = ThreadingHTTPServer(("localhost", 0), dashboard.DashboardHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        dashboard.DB_PATH = self._orig_db
        scanner.SESSIONS_DIR = self._orig_dir
        if os.path.exists(self.db):
            os.unlink(self.db)

    def _get(self, path):
        with urllib.request.urlopen(f"http://localhost:{self.port}{path}") as r:
            return r.status, r.read()

    def _post(self, path):
        req = urllib.request.Request(f"http://localhost:{self.port}{path}", method="POST")
        with urllib.request.urlopen(req) as r:
            return r.status, r.read()

    def test_index_serves_html_with_config(self):
        status, body = self._get("/")
        self.assertEqual(status, 200)
        html = body.decode("utf-8")
        self.assertIn("Codex / GPT Usage", html)
        self.assertNotIn("__APP_CONFIG_JSON__", html)  # placeholder was substituted

    def test_api_data_uses_patched_db(self):
        status, body = self._get("/api/data")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["all_models"])

    def test_api_rescan_incremental_and_targets_fixtures(self):
        status, body = self._post("/api/rescan")
        self.assertEqual(status, 200)
        result = json.loads(body)
        # DB already seeded from fixtures -> a rescan should skip all, add 0 turns
        # (proves it hit the patched DB + fixtures dir, not the real ~/.codex).
        self.assertEqual(result["turns"], 0)
        self.assertGreater(result["skipped"], 0)

    def test_404(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._get("/nope")
        self.assertEqual(cm.exception.code, 404)


if __name__ == "__main__":
    unittest.main()

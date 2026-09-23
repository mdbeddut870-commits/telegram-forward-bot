"""Dashboard endpoint tests: auth, pages, API, toggle. Stdlib only."""

import http.client
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import database as db
import bot as botmod


class DashboardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls._old_db = db.DB_PATH
        cls._old_tok = config.DASHBOARD_TOKEN
        db.DB_PATH = str(Path(cls.tmp.name) / "test.db")
        config.DASHBOARD_TOKEN = "test-secret"
        db.init_db()
        cls.mid = db.add_mapping(-100111, -100222, "SrcChan", "DstChan")
        from http.server import ThreadingHTTPServer
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), botmod._HealthHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        db.DB_PATH = cls._old_db
        config.DASHBOARD_TOKEN = cls._old_tok
        cls.tmp.cleanup()

    def request(self, method, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, headers=headers or {})
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", "replace")
        result = (resp.status, resp.getheaders(), body)
        conn.close()
        return result

    def _ensure_active(self):
        row = db.get_mapping(self.mid)
        if not row["active"]:
            db.toggle_mapping(self.mid)

    def test_ping_public(self):
        status, _, body = self.request("GET", "/ping")
        self.assertEqual(status, 200)
        self.assertIn("running", body)

    def test_login_redirect_sets_cookie(self):
        status, headers, _ = self.request("GET", "/?token=test-secret")
        self.assertEqual(status, 303)
        cookies = [v for k, v in headers if k.lower() == "set-cookie"]
        self.assertTrue(any("dash_token=test-secret" in c for c in cookies))

    def test_bad_token_denied(self):
        status, _, _ = self.request("GET", "/?token=wrong")
        self.assertEqual(status, 403)
        status2, _, _ = self.request("GET", "/")
        self.assertEqual(status2, 403)

    def test_disabled_when_no_token_configured(self):
        saved = config.DASHBOARD_TOKEN
        config.DASHBOARD_TOKEN = ""
        try:
            status, _, body = self.request("GET", "/")
        finally:
            config.DASHBOARD_TOKEN = saved
        self.assertEqual(status, 403)
        self.assertIn("DASHBOARD_TOKEN", body)

    def test_page_with_cookie(self):
        status, _, body = self.request(
            "GET", "/", {"Cookie": "dash_token=test-secret"})
        self.assertEqual(status, 200)
        self.assertIn("Overview", body)
        self.assertIn("SrcChan", body)
        self.assertIn("pause", body)

    def test_api_bearer_and_endpoints(self):
        auth = {"Authorization": "Bearer test-secret"}
        status, _, body = self.request("GET", "/api/mappings", auth)
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["mappings"][0]["id"], self.mid)
        status2, _, _ = self.request("GET", "/api/mappings")
        self.assertEqual(status2, 401)
        status3, _, body3 = self.request("GET", "/api/stats?days=7", auth)
        self.assertEqual(status3, 200)
        self.assertIn("forwarded", json.loads(body3))
        db.check_and_mark_seen("abc123hash", -100111)
        status4, _, body4 = self.request("GET", "/api/seen", auth)
        self.assertEqual(status4, 200)
        self.assertIn("abc123hash", body4)

    def test_toggle_pause_resume_json(self):
        self._ensure_active()
        auth = {"Authorization": "Bearer test-secret",
                "Accept": "application/json"}
        path = f"/api/mappings/{self.mid}/toggle"
        status, _, body = self.request("POST", path, auth)
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["active"])
        status2, _, body2 = self.request("POST", path, auth)
        self.assertEqual(status2, 200)
        self.assertTrue(json.loads(body2)["active"])

    def test_toggle_browser_redirect(self):
        self._ensure_active()
        path = f"/api/mappings/{self.mid}/toggle"
        status, headers, _ = self.request(
            "POST", path, {"Cookie": "dash_token=test-secret"})
        self.assertEqual(status, 303)
        self.assertFalse(db.get_mapping(self.mid)["active"])
        db.toggle_mapping(self.mid)

    def test_toggle_unauthorized(self):
        status, _, _ = self.request("POST", f"/api/mappings/{self.mid}/toggle")
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main(verbosity=2)

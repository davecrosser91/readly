"""v0.2c: Token-Auth für Remote-Zugriffe + OpenAPI-Spec.

"Remote" heißt: der Request kam durch einen Proxy/Tunnel (X-Forwarded-For
gesetzt) oder von einer Nicht-Loopback-Adresse. Direkte localhost-Nutzung
(App, lokale Agenten) bleibt tokenfrei.
"""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server  # noqa: E402


def _get(port, path, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode()), r.headers
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}"), e.headers


class RemoteAuthTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        server.DATA_DIR = cls.tmp.name
        server.DB_PATH = os.path.join(cls.tmp.name, "lector.db")
        server.EVENTS_PATH = os.path.join(cls.tmp.name, "events.jsonl")
        server.TOKEN_PATH = os.path.join(cls.tmp.name, "token")
        server._TOKEN = None
        with server.db() as conn:
            conn.executescript(server.SCHEMA)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.token = server.load_token()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.tmp.cleanup()

    def test_token_file_created_and_stable(self):
        self.assertTrue(os.path.isfile(server.TOKEN_PATH))
        self.assertGreaterEqual(len(self.token), 32)
        self.assertEqual(server.load_token(), self.token)

    def test_localhost_needs_no_token(self):
        status, body, _ = _get(self.port, "/api/books")
        self.assertEqual(status, 200)

    def test_proxied_request_without_token_is_401(self):
        status, body, _ = _get(self.port, "/api/books",
                               {"X-Forwarded-For": "203.0.113.7"})
        self.assertEqual(status, 401)

    def test_proxied_request_with_bearer_token_ok(self):
        status, body, _ = _get(self.port, "/api/books",
                               {"X-Forwarded-For": "203.0.113.7",
                                "Authorization": "Bearer " + self.token})
        self.assertEqual(status, 200)

    def test_wrong_token_is_401(self):
        status, _, _ = _get(self.port, "/api/books",
                            {"X-Forwarded-For": "203.0.113.7",
                             "Authorization": "Bearer falsch"})
        self.assertEqual(status, 401)

    def test_query_token_works_and_sets_cookie(self):
        status, _, headers = _get(self.port, "/api/books?token=" + self.token,
                                  {"X-Forwarded-For": "203.0.113.7"})
        self.assertEqual(status, 200)
        self.assertIn("lector_token=", headers.get("Set-Cookie", ""))

    def test_cookie_authorizes(self):
        status, _, _ = _get(self.port, "/api/books",
                            {"X-Forwarded-For": "203.0.113.7",
                             "Cookie": "lector_token=" + self.token})
        self.assertEqual(status, 200)

    def test_post_requires_token_when_proxied(self):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/position",
            data=json.dumps({"book_id": 1, "chapter_idx": 0, "para_idx": 0}).encode(),
            headers={"Content-Type": "application/json",
                     "X-Forwarded-For": "203.0.113.7"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                status = r.status
        except urllib.error.HTTPError as e:
            status = e.code
        self.assertEqual(status, 401)

    def test_openapi_is_public_and_describes_agent_api(self):
        status, spec, _ = _get(self.port, "/openapi.json",
                               {"X-Forwarded-For": "203.0.113.7",
                                "X-Forwarded-Proto": "https",
                                "X-Forwarded-Host": "mac.tailnet.ts.net"})
        self.assertEqual(status, 200)
        self.assertTrue(spec["openapi"].startswith("3."))
        self.assertEqual(spec["servers"][0]["url"], "https://mac.tailnet.ts.net")
        for p in ["/api/books", "/api/agent/next", "/api/agent/push", "/api/vocab"]:
            self.assertIn(p, spec["paths"], p)
        schemes = spec["components"]["securitySchemes"]
        self.assertIn("bearer", json.dumps(schemes).lower())


if __name__ == "__main__":
    unittest.main()

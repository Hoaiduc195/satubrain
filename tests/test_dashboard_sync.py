from http.client import HTTPConnection
from importlib.util import find_spec
import json
from pathlib import Path
from secrets import token_urlsafe
import tempfile
from threading import Thread
import unittest

from satubrain.dashboard import make_dashboard
from satubrain.scope import Workspace
from satubrain.store import Store
from satubrain.sync import pull, push


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "db.sqlite3")
        self.server, self.token = make_dashboard(self.store, Workspace("portable", "main"), "DEMO-17")
        self.addCleanup(self.server.server_close)
        thread = Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)
        self.origin = f"http://127.0.0.1:{self.server.server_port}"

    def request(self, path, payload=None, headers=None):
        client = HTTPConnection("127.0.0.1", self.server.server_port)
        method = "GET" if payload is None else "POST"
        try:
            defaults = {"X-SatuBrain-Key": self.token}
            if payload is not None:
                defaults.update({"Origin": self.origin, "Content-Type": "application/json"})
            client.request(method, path, None if payload is None else json.dumps(payload),
                           defaults | (headers or {}))
            response = client.getresponse()
            return response.status, response.read(), response.headers
        finally:
            client.close()

    def test_route_authorization_and_response_headers(self):
        self.assertEqual(self.request("/api/scopes", headers={"X-SatuBrain-Key": "bad"})[0], 403)
        self.assertEqual(self.request("/api/scopes", headers={"Host": "invalid.test"})[0], 403)
        self.assertEqual(self.request("/api/memories", {"text": "note"}, {"Origin": "http://example.test"})[0], 403)
        self.assertEqual(self.request("/api/memories?project=another")[0], 403)
        status, html, headers = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(b"SatuBrain", html)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertNotIn(self.token.encode(), html)

    def test_dashboard_capture_search_detail_and_delete(self):
        text = "Queue <pending> branch"
        status, result, _ = self.request("/api/memories", {"kind": "note", "text": text})
        self.assertEqual(status, 200)
        identifier = json.loads(result)["id"]
        items = json.loads(self.request("/api/memories?query=Queue")[1])["items"]
        self.assertEqual([item["id"] for item in items], [identifier])
        status, detail, _ = self.request("/api/memory?id=" + identifier)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(detail)["item"]["text"], text)
        self.assertEqual(self.request("/api/forget", {"id": identifier})[0], 200)
        self.assertEqual(self.request("/api/memory?id=" + identifier)[0], 404)


@unittest.skipUnless(find_spec("cryptography"), "requires the optional sync dependency")
class SyncTests(unittest.TestCase):
    def test_two_device_encrypted_merge_deletion_and_authentication(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            shared = root / "shared"
            shared.mkdir()
            first, second = Store(root / "first.sqlite3"), Store(root / "second.sqlite3")
            scope = ("portable", "DEMO-17", "main")
            secret = token_urlsafe(32)
            result = first.save(project=scope[0], task=scope[1], branch=scope[2],
                                kind="note", text="private project details")
            snapshot = shared / push(first, shared, secret)["file"]
            self.assertNotIn(b"private project details", snapshot.read_bytes())
            self.assertEqual(pull(second, shared, secret)["added"], 1)
            with self.assertRaisesRegex(ValueError, "authentication failed"):
                pull(second, shared, token_urlsafe(32))
            before = snapshot.read_bytes()
            snapshot.write_bytes(before[:-1] + bytes([before[-1] ^ 1]))
            with self.assertRaisesRegex(ValueError, "authentication failed"):
                pull(second, shared, secret)
            self.assertEqual(len(second.search(*scope)), 1)
            snapshot.write_bytes(before)
            self.assertTrue(second.delete(*scope, result["id"]))
            push(second, shared, secret)
            self.assertEqual(pull(first, shared, secret)["tombstones"], 1)
            self.assertEqual(first.search(*scope), [])

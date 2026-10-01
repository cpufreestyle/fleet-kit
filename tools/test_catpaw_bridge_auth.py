"""CatPaw must enforce the bridge key the installer already generates.

install.sh mints CATPAW2CODEX_KEY, writes it into runtime/fleet.env and exports
it in com.local.catpaw2codex.plist -- and catpaw_bridge.py never read it. Every
other bridge calls check_bridge_auth(); this one had no authorization check at
all, so any local caller could spend the operator Meituan quota. The listener
is on 127.0.0.1, so this is about local processes and other users on the box,
not the internet -- which is exactly the same trust model the other twelve
bridges already pay for.

/health stays open, matching every FastAPI sibling: it is a liveness probe and
is called without a key by the fleet tooling.
"""
import importlib.util
import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

BRIDGE_DIR = os.path.abspath(os.path.join(
    os.path.dirname(__file__), os.pardir, "bridges", "catpaw"))
sys.path.insert(0, BRIDGE_DIR)

# BRIDGE_KEY is read at import time, so the key has to be in the environment
# before the module is loaded, the same way launchd provides it.
TEST_KEY = "sk-local-catpaw-test-key"
os.environ["CATPAW2CODEX_KEY"] = TEST_KEY

spec = importlib.util.spec_from_file_location(
    "catpaw_auth", os.path.join(BRIDGE_DIR, "catpaw_bridge.py"))
cp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cp)


class _Server:
    def __init__(self):
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), cp.H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def get(self, path, key=None):
        headers = {"Authorization": "Bearer " + key} if key else {}
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            body = e.read() or b"{}"
            try:
                return e.code, json.loads(body)
            except Exception:
                return e.code, {"raw": body[:120].decode("utf-8", "replace")}

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class CatpawBridgeAuthTest(unittest.TestCase):
    def setUp(self):
        self.srv = _Server()
        self.addCleanup(self.srv.close)

    def test_the_key_is_read_from_the_environment(self):
        self.assertTrue(cp.BRIDGE_KEY, "CATPAW2CODEX_KEY must be honoured")
        self.assertEqual(cp.BRIDGE_KEY, TEST_KEY)

    def test_models_requires_the_key(self):
        code, _ = self.srv.get("/v1/models")
        self.assertEqual(code, 401)

    def test_models_rejects_a_wrong_key(self):
        code, _ = self.srv.get("/v1/models", key="sk-local-wrong")
        self.assertEqual(code, 401)

    def test_models_accepts_the_right_key(self):
        code, body = self.srv.get("/v1/models", key=TEST_KEY)
        self.assertEqual(code, 200)
        self.assertIn("data", body)

    def test_health_stays_open(self):
        """Every FastAPI sibling leaves /health unauthenticated."""
        code, _ = self.srv.get("/health")
        self.assertEqual(code, 200)


if __name__ == "__main__":
    unittest.main()

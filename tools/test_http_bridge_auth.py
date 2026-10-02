"""Every BaseHTTPRequestHandler bridge must enforce the key the installer mints.

fleet_probe.py documents the invariant in its own comment: KEY_ENV is "local
token each bridge checks, as found in its launchd plist". install.sh mints one
per bridge, writes it to runtime/fleet.env and exports it in the plist. Two of the
three http.server bridges -- gemini and antigravity -- never read theirs, so any
local caller could list the model catalog and, worse, reach the upstream: an
unauthenticated POST /v1/chat/completions to antigravity answers 502 after 1.8s,
which means it already burned an OAuth refresh before refusing.

catpaw was the same defect and is already fixed; these tests cover the two that
were still open, so the whole http.server family is pinned in one place.

/health stays open, matching every FastAPI sibling -- it is a liveness probe and
the fleet tooling calls it without a key.
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

BRIDGES_DIR = os.path.abspath(os.path.join(
    os.path.dirname(__file__), os.pardir, "bridges"))
sys.path.insert(0, BRIDGES_DIR)

KEY = "sk-local-http-bridge-test"
TARGETS = [
    ("gemini", "gemini", "gemini_bridge.py", "GEMINI2CODEX_KEY",
     "google-one", "/v1/chat/completions"),
    ("antigravity", "antigravity", "antigravity_bridge.py",
     "ANTIGRAVITY2CODEX_KEY", "google-antigravity", "/v1/chat/completions"),
]

MODULES = {}
for name, sub, filename, env, _owned, _path in TARGETS:
    os.environ[env] = KEY
    spec = importlib.util.spec_from_file_location(
        "http_bridge_auth_" + name, os.path.join(BRIDGES_DIR, sub, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    MODULES[name] = (mod, env)


class _Server:
    def __init__(self, handler):
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def request(self, method, path, key=None, body=None):
        headers = {}
        if key:
            headers["Authorization"] = "Bearer " + key
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path),
            data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return r.status
        except urllib.error.HTTPError as e:
            e.read()
            return e.code
        except Exception as exc:
            raise AssertionError("%s %s -> %s" % (method, path, exc))

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def _cases():
    out = []
    for name, (mod, env) in MODULES.items():
        out.append((name, mod, env))
    return out


class HttpBridgeAuthTest(unittest.TestCase):
    def setUp(self):
        self.servers = []
        for _name, mod, _env in _cases():
            srv = _Server(mod.H)
            self.servers.append((mod, srv))
            self.addCleanup(srv.close)

    def _one(self, name):
        for mod, srv in self.servers:
            if mod in MODULES[name][0:1] or MODULES[name][0] is mod:
                return mod, srv
        raise AssertionError("no server for " + name)

    def test_the_key_is_read_from_the_environment(self):
        for name, (mod, env) in MODULES.items():
            self.assertEqual(getattr(mod, "BRIDGE_KEY", None), KEY,
                             "%s must honour %s" % (name, env))

    def test_models_requires_the_key(self):
        for name, _ in MODULES.items():
            _mod, srv = self._one(name)
            self.assertEqual(srv.request("GET", "/v1/models"), 401, name)

    def test_models_rejects_a_wrong_key(self):
        for name, _ in MODULES.items():
            _mod, srv = self._one(name)
            self.assertEqual(
                srv.request("GET", "/v1/models", key="sk-local-wrong"), 401, name)

    def test_models_accepts_the_right_key(self):
        for name, _ in MODULES.items():
            _mod, srv = self._one(name)
            self.assertEqual(srv.request("GET", "/v1/models", key=KEY), 200, name)

    def test_chat_requires_the_key(self):
        for name, _ in MODULES.items():
            _mod, srv = self._one(name)
            self.assertEqual(
                srv.request("POST", "/v1/chat/completions",
                             body={"model": "x", "messages": []}), 401, name)

    def test_health_stays_open(self):
        for name, _ in MODULES.items():
            _mod, srv = self._one(name)
            self.assertEqual(srv.request("GET", "/health"), 200, name)


if __name__ == "__main__":
    unittest.main()

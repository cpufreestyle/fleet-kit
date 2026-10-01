"""Last-resort guard for the BaseHTTPRequestHandler services.

http.server has no exception-handler stage. When do_POST raises,
socketserver prints a traceback and closes the socket: the client sees a
reset, not an error. gemini and antigravity both reached that state through
one unguarded json.loads of the request body. install_basehttp_guard wraps
every do_* method so an escaped exception still leaves an envelope; a source
scan keeps any new http.server service honest, and an import test keeps the
wrap call itself from silently breaking (a bare name once passed py_compile
and died with NameError on the first restart).
"""
import importlib.util
import json
import os
import re
import sys
import unittest

KIT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
BRIDGES = os.path.join(KIT, "bridges")


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(KIT, rel))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


basehttp = _load("_basehttp_under_test", os.path.join("bridges", "_basehttp.py"))


class FakeHandler:
    """Minimal stand-in for a BaseHTTPRequestHandler bridge."""

    def __init__(self, send_error=None):
        self.sent = []
        self.send_error = send_error

    def _send(self, code, obj, hdrs=None):
        if self.send_error is not None:
            raise self.send_error
        self.sent.append((code, obj))

    def do_POST(self):
        raise json.JSONDecodeError("Expecting value", "<body>", 0)

    def do_GET(self):
        return "fine"


class BaseHTTPGuardTests(unittest.TestCase):
    def setUp(self):
        # The guard wraps do_* in place, so anything a test adds to the class
        # (do_PUT, a fresh do_POST) must not leak into the next test: snapshot
        # the class and put it back afterwards.
        snapshot = dict(vars(FakeHandler))

        def restore():
            for name in list(vars(FakeHandler)):
                if name not in snapshot:
                    delattr(FakeHandler, name)
            for name, value in snapshot.items():
                if name.startswith("__"):
                    continue  # __dict__/__weakref__ slots are not writable
                setattr(FakeHandler, name, value)

        self.addCleanup(restore)

    def test_malformed_body_leaves_400_envelope(self):
        guarded = basehttp.install_basehttp_guard(FakeHandler)
        handler = FakeHandler()
        guarded.do_POST(handler)
        self.assertEqual(len(handler.sent), 1)
        code, body = handler.sent[0]
        self.assertEqual(code, 400)
        payload = json.loads(body)
        self.assertEqual(payload["error"]["type"], "bad_request")
        self.assertIn("Expecting value", payload["error"]["message"])

    def test_unexpected_error_leaves_500_envelope(self):
        def boom(self):
            raise RuntimeError("upstream exploded")

        FakeHandler.do_PUT = boom
        guarded = basehttp.install_basehttp_guard(FakeHandler)
        handler = FakeHandler()
        guarded.do_PUT(handler)
        code, body = handler.sent[0]
        self.assertEqual(code, 500)
        self.assertEqual(json.loads(body)["error"]["type"], "bridge_error")

    def test_normal_responses_pass_through(self):
        guarded = basehttp.install_basehttp_guard(FakeHandler)
        handler = FakeHandler()
        self.assertEqual(guarded.do_GET(handler), "fine")
        self.assertEqual(handler.sent, [])

    def test_double_wrap_produces_one_envelope(self):
        guarded = basehttp.install_basehttp_guard(FakeHandler)
        guarded = basehttp.install_basehttp_guard(guarded)
        handler = FakeHandler()
        guarded.do_POST(handler)
        self.assertEqual(len(handler.sent), 1)
        self.assertEqual(handler.sent[0][0], 400)

    def test_send_failure_stays_silent(self):
        guarded = basehttp.install_basehttp_guard(FakeHandler)
        handler = FakeHandler(send_error=BrokenPipeError("gone"))
        self.assertIsNone(guarded.do_POST(handler))

    def test_every_basehttp_handler_is_guarded(self):
        """No http.server service may ship without the last-resort envelope.

        One-shot helpers stay exempt: lingxi/login_helper.py answers an
        OAuth callback through handle_request() in a loop and exits on its
        own, so a crash there is loud on purpose. A file counts as a service
        when it is a bridge (*_bridge.py) or calls serve_forever() -- the
        latter also pulls in zcode/captcha-relay.py, which runs under a bare
        python3 and shares the guard through the stdlib-only _basehttp.
        """
        handler_class = re.compile(r"class\s+\w+\(BaseHTTPRequestHandler\)")
        guarded_files = 0
        for root, dirs, files in os.walk(BRIDGES):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for fname in files:
                if not fname.endswith(".py"):
                    continue
                path = os.path.join(root, fname)
                with open(path, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
                if not handler_class.search(text):
                    continue
                is_service = fname.endswith("_bridge.py") or "serve_forever" in text
                if not is_service:
                    continue
                self.assertIn(
                    "install_basehttp_guard(", text,
                    f"{path} defines a BaseHTTPRequestHandler without the guard",
                )
                guarded_files += 1
        self.assertGreaterEqual(guarded_files, 3)

    def test_http_server_services_import_and_wrap(self):
        """Every guarded service must import cleanly with its do_POST wrapped.

        The first cut called install_basehttp_guard as a bare name after
        import _common: py_compile passed, every restart died with NameError.
        Importing the real services keeps that class of mistake red.
        """
        services = [
            "bridges/antigravity/antigravity_bridge.py",
            "bridges/catpaw/catpaw_bridge.py",
            "bridges/gemini/gemini_bridge.py",
            "bridges/zcode/captcha-relay.py",
        ]
        for rel in services:
            mod = _load(os.path.basename(rel)[:-3].replace("-", "_") + "_service", rel)
            handler = getattr(mod, "H", None) or getattr(mod, "Handler", None)
            self.assertIsNotNone(handler, f"{rel}: no handler class exported")
            self.assertTrue(
                getattr(getattr(handler, "do_POST", None), "_basehttp_guard", False),
                f"{rel}: do_POST is not wrapped",
            )


if __name__ == "__main__":
    unittest.main()


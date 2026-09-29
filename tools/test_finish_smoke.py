"""finish.sh must not call a bridge ready when its chat call fails.

Measured 2026-09-29: the smoke chat printed the first 600 bytes of the
response and then always announced "done: <name> is ready", so a bridge that
was listening, listed its models and answered 401 with {"error":...} was
reported as working. The script now keeps the status code next to the body and
exits 4 when the call fails or the body carries an error object.
"""
import json
import os
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

FINISH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "bridges", "finish.sh")
PORT_BASE = 18887          # qoder sits at PORT_BASE + 2
PORT = PORT_BASE + 2

PLAN = {"chat_code": 200, "chat_body": ""}
OK_BODY = json.dumps({
    "id": "chatcmpl-1", "object": "chat.completion",
    "choices": [{"index": 0, "finish_reason": "stop",
                 "message": {"role": "assistant", "content": "正常"}}]})
ERR_401 = json.dumps({"error": {"message": "session expired",
                                "type": "auth_error"}})
ERR_200 = json.dumps({"error": {"message": "upstream refused",
                                "type": "upstream_error"},
                      "id": "chatcmpl-2"})


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, code, body):
        payload = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/v1/models":
            self._json(200, json.dumps(
                {"object": "list", "data": [{"id": "qoder/Auto",
                                             "object": "model"}]}))
            return
        self._json(404, json.dumps({"error": {"message": "no such route"}}))

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if self.path != "/v1/chat/completions":
            self._json(404, json.dumps({"error": {"message": "no such route"}}))
            return
        self._json(PLAN["chat_code"], PLAN["chat_body"])


def _home():
    home = tempfile.mkdtemp(prefix="fk-finish-")
    with open(os.path.join(home, "fleet.env"), "w", encoding="utf-8") as fh:
        fh.write("PORT_BASE=%d\nQODER2CODEX_KEY=sk-test-qoder\n" % PORT_BASE)
    tools = os.path.join(home, "tools")
    os.makedirs(tools)
    with open(os.path.join(tools, "platform.sh"), "w", encoding="utf-8") as fh:
        fh.write("# test stub: no launchd, no scheduler\n"
                 "fleet_service_dir() { echo \"$HOME/Library/LaunchAgents\"; }\n"
                 "fleet_system_python() { echo /usr/bin/python3; }\n"
                 "fleet_os() { echo macos; }\n"
                 "fleet_service_status() { echo running; }\n"
                 "fleet_service_restart() { return 0; }\n")
    os.makedirs(os.path.join(home, "bridges", "qoder"))
    return home


def _run(home):
    env = dict(os.environ)
    env["HOME"] = home            # keep LaunchAgents lookups inside the sandbox
    env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"   # no ocx on the path
    env.pop("CODEX_HOME", None)
    return subprocess.run(["/bin/bash", FINISH, "qoder", "--home", home,
                           "--tries", "3"],
                          env=env, capture_output=True, text=True, timeout=120)


def test_smoke_chat_success_reports_ready():
    server = HTTPServer(("127.0.0.1", PORT), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        PLAN["chat_code"], PLAN["chat_body"] = 200, OK_BODY
        home = _home()
        proc = _run(home)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "ok (HTTP 200)" in proc.stdout
        assert "done: qoder is ready" in proc.stdout
        assert "FAILED" not in proc.stdout + proc.stderr
    finally:
        server.shutdown()
        server.server_close()


def test_smoke_chat_401_is_not_ready():
    server = HTTPServer(("127.0.0.1", PORT), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        PLAN["chat_code"], PLAN["chat_body"] = 401, ERR_401
        home = _home()
        proc = _run(home)
        assert proc.returncode == 4, proc.stdout + proc.stderr
        assert "chat call FAILED (HTTP 401)" in proc.stderr
        assert "session expired" in proc.stderr
        # The failure is stated, not papered over with a success line.
        assert "done: qoder is ready" not in proc.stdout
    finally:
        server.shutdown()
        server.server_close()


def test_smoke_chat_200_with_an_error_body_is_not_ready():
    server = HTTPServer(("127.0.0.1", PORT), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        PLAN["chat_code"], PLAN["chat_body"] = 200, ERR_200
        home = _home()
        proc = _run(home)
        assert proc.returncode == 4, proc.stdout + proc.stderr
        assert "chat call FAILED (HTTP 200)" in proc.stderr
        assert "upstream refused" in proc.stderr
    finally:
        server.shutdown()
        server.server_close()


def test_skip_chat_does_not_claim_a_verdict():
    server = HTTPServer(("127.0.0.1", PORT), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        PLAN["chat_code"], PLAN["chat_body"] = 200, OK_BODY
        home = _home()
        env = dict(os.environ)
        env["HOME"] = home
        env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
        proc = subprocess.run(
            ["/bin/bash", FINISH, "qoder", "--home", home, "--tries", "3",
             "--skip-chat"],
            env=env, capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "skipped (--skip-chat)" in proc.stdout
    finally:
        server.shutdown()
        server.server_close()

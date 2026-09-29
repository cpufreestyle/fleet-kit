import asyncio
import http.server
import importlib.util
import json
import os
import socket
import sys
import tempfile
import threading
from pathlib import Path

import httpx
import pytest

spec = importlib.util.spec_from_file_location("checkin", "tools/checkin.py")
checkin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checkin)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fleet_platform  # noqa: E402


def health_handler(request):
        assert request.url.path == "/health"
        assert request.headers.get("authorization") == "Bearer test-key"
        return httpx.Response(200, json={"status": "ok"})


def test_workbuddy_health(monkeypatch):
    monkeypatch.setattr(checkin, "WORKBUDDY_HEALTH_URL", "http://127.0.0.1:1/health")
    monkeypatch.setattr(checkin, "WORKBUDDY_KEY", "test-key")
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(health_handler)) as ac:
            return await checkin.task_workbuddy(ac)
    import asyncio
    result = asyncio.run(run())
    assert result["ok"] is True
    assert "自动签到服务在线" in result["detail"]

def test_task_registry_contains_both_tasks():
    assert set(checkin.TASKS) == {"xhx", "workbuddy"}


class _HealthHandler(http.server.BaseHTTPRequestHandler):
    """A stand-in for the workbuddy-gpt bridge /health endpoint."""

    def do_GET(self):
        assert self.path == "/health"
        assert self.headers.get("authorization") == "Bearer test-key"
        body = b'{"status":"ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _dead_port():
    """A loopback port nobody listens on (the stand-in for a broken proxy)."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_the_loopback_health_call_survives_a_proxy_environment(monkeypatch):
    """Regression: HTTP_PROXY must not swallow the bridge health check.

    Measured 2026-09-29: under a shell with HTTP_PROXY set the task failed with
    httpx.ConnectError("All connection attempts failed") for 127.0.0.1:8788
    while the very same call without the proxy env answered 200. The client
    built by run_tasks now carries LOOPBACK_MOUNTS, so this drives the real
    client (not a mock transport) against a real server and a dead proxy.
    """
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:%d" % _dead_port())
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:%d" % _dead_port())
    monkeypatch.setattr(checkin, "WORKBUDDY_HEALTH_URL",
                        "http://127.0.0.1:%d/health" % port)
    monkeypatch.setattr(checkin, "WORKBUDDY_KEY", "test-key")

    home = Path(tempfile.mkdtemp())
    monkeypatch.setattr(checkin, "HOME", home)
    monkeypatch.setattr(checkin, "STATE_FILE", home / "state.json")
    monkeypatch.setattr(checkin, "LOG_FILE", home / "checkin.log")
    monkeypatch.setattr(checkin, "TASKS", {"workbuddy": {"desc": "test", "fn": checkin.task_workbuddy}})

    try:
        rc = asyncio.run(checkin.run_tasks(["workbuddy"]))
        assert rc == 0
        state = json.loads((home / "state.json").read_text(encoding="utf-8"))
        assert state["workbuddy"]["ok"] is True
    finally:
        server.shutdown()
        server.server_close()


def test_the_bridge_key_falls_back_to_the_service_definition(monkeypatch):
    """The daily timer env has no CODEBUDDY2OPENAI_KEY; the plist does.

    Measured 2026-09-28/29: every 09:00 run failed with HTTP 401 "invalid api
    key" while manual runs (which source fleet.env) succeeded.
    """
    monkeypatch.delenv("CODEBUDDY2OPENAI_KEY", raising=False)
    monkeypatch.setattr(checkin, "WORKBUDDY_KEY", "")
    monkeypatch.setattr(fleet_platform, "service_keys",
                        lambda: {"com.local.workbuddy2codex-gpt": "sk-from-plist",
                                 "com.local.trae2codex": "sk-other-bridge"})
    assert checkin.workbuddy_bridge_key() == "sk-from-plist"


def test_the_environment_key_still_wins(monkeypatch):
    monkeypatch.setenv("CODEBUDDY2OPENAI_KEY", "sk-from-env")
    monkeypatch.setattr(checkin, "WORKBUDDY_KEY", "")
    monkeypatch.setattr(fleet_platform, "service_keys",
                        lambda: {"com.local.workbuddy2codex-gpt": "sk-from-plist"})
    assert checkin.workbuddy_bridge_key() == "sk-from-env"

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
spec_nc = importlib.util.spec_from_file_location(
    "node_credits", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "node_credits.py"))
node_credits = importlib.util.module_from_spec(spec_nc)
spec_nc.loader.exec_module(node_credits)
import fleet_platform  # noqa: E402


def test_the_registry_covers_every_node_in_the_fleet():
    """A node added to fleet_probe cannot be missing from the daily run.

    Measured 2026-10-01: the registry held 2 of 15 nodes, so "签到和积分" was
    a question the other thirteen could not answer at all.
    """
    import fleet_probe
    every = set(fleet_probe.PORTS) | set(fleet_probe.GATEWAY)
    assert set(checkin.TASKS) == every, sorted(every - set(checkin.TASKS))
    # derived, not a remembered number: the set equality above is what
    # forces the registry to grow with the fleet
    assert len(checkin.TASKS) == len(every)
    # the nodes that really claim must not share the placeholder task
    placeholder = checkin.TASKS["qoder"]["fn"]
    for name in ("xhx", "workbuddy", "workbuddy-gpt"):
        assert checkin.TASKS[name]["fn"] is not placeholder, name
    assert len({checkin.TASKS[n]["fn"]
                for n in ("xhx", "workbuddy", "workbuddy-gpt")}) == 3

class _WorkbuddyBridge(http.server.BaseHTTPRequestHandler):
    """The bridge routes the Buddy 加油站 task really uses."""

    SESSION = "test-session"
    claimed = []

    def _json(self, payload, code=200, cookie=False):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if cookie:
            self.send_header("Set-Cookie",
                             "workbuddy_bridge_session=%s; Path=/" % self.SESSION)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            self._json({"bridge": "workbuddy2codex"}, cookie=True)
            return
        if self.path == "/ui/checkin":
            self._json({
                "ok": True,
                "activity": {"active": True, "theme_name": "Buddy加油站",
                             "daily_credit": 100.0},
                "accounts": [{"ref": "r1", "name": "MichaelQiu", "ok": True,
                             "today_checked_in": False, "claimed": False,
                             "credit": 100.0, "streak_days": 0, "message": ""}],
                "unclaimed_count": 1, "any_unclaimed": True})
            return
        self._json({"detail": "Not Found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if self.path == "/ui/checkin/claim":
            type(self).claimed.append(self.headers.get("Cookie"))
            self._json({"ok": True, "claimed_accounts": 1, "claimed_total": 100.0})
            return
        self._json({"detail": "Not Found"}, 404)

    def log_message(self, *args):
        pass


def test_the_daily_task_really_claims_through_the_bridge(monkeypatch, tmp_path):
    """Buddy 加油站 is claimed, not merely observed.

    Measured 2026-10-01 on 127.0.0.1:8787: GET /ui/checkin answers the real
    activity and per-account state, and POST /ui/checkin/claim takes it. The
    old task only confirmed the bridge was reachable, so a pool with an
    unclaimed account reported success every day while the credit went
    uncollected. This drives the same three routes against a local stand-in
    and asserts the claim carries the session cookie the bridge handed out.
    """
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _WorkbuddyBridge)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(checkin.node_credits, "PORTS",
                        dict(checkin.node_credits.PORTS, workbuddy=port))
    monkeypatch.setattr(checkin, "HOME", tmp_path)
    monkeypatch.setattr(checkin, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(checkin, "LOG_FILE", tmp_path / "checkin.log")
    try:
        rc = asyncio.run(checkin.run_tasks(["workbuddy"]))
        assert rc == 0
        state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
        assert state["workbuddy"]["ok"] is True
        assert state["workbuddy"]["granted"] is True
        assert state["workbuddy"]["available_points"] == 100.0
        assert state["workbuddy"]["streak_days"] == 0
        # the claim went out with the cookie collected from GET /
        assert _WorkbuddyBridge.claimed == ["workbuddy_bridge_session=test-session"]
    finally:
        server.shutdown()
        server.server_close()
        _WorkbuddyBridge.claimed.clear()


def test_an_unclaimable_account_is_not_reported_as_checked_in(monkeypatch, tmp_path):
    """A pool whose status cannot be read fails loudly.

    Measured 2026-10-01: the overseas bridge asked the CN backend with a
    www.workbuddy.ai credential and got 401 Authorization Required. The task
    must not turn that into "today is done".
    """
    class _Broken(_WorkbuddyBridge):
        def do_GET(self):
            if self.path == "/":
                return _WorkbuddyBridge.do_GET(self)
            self._json({"ok": True,
                        "activity": {"active": False, "daily_credit": 0},
                        "accounts": [{"ref": "r1", "name": "overseas",
                                      "ok": False, "today_checked_in": False,
                                      "claimed": False, "credit": 0,
                                      "streak_days": 0,
                                      "message": "状态读取失败：401"}],
                        "unclaimed_count": 0, "any_unclaimed": False})

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Broken)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(checkin.node_credits, "PORTS",
                        dict(checkin.node_credits.PORTS,
                             **{"workbuddy-gpt": port}))
    monkeypatch.setattr(checkin, "HOME", tmp_path)
    monkeypatch.setattr(checkin, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(checkin, "LOG_FILE", tmp_path / "checkin.log")
    try:
        rc = asyncio.run(checkin.run_tasks(["workbuddy-gpt"]))
        assert rc == 1
        state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
        assert state["workbuddy-gpt"]["ok"] is False
        assert "401" in state["workbuddy-gpt"]["detail"]
        assert "last_success_date" not in state["workbuddy-gpt"]
    finally:
        server.shutdown()
        server.server_close()


def test_a_node_without_an_endpoint_is_not_a_failure(monkeypatch, tmp_path):
    """na is its own state: the node has no such endpoint to call.

    Twelve of the fifteen nodes have no daily check-in endpoint. Marking them
    failed would make the 09:00 timer report red every single day for a
    condition nobody can fix, which trains the operator to ignore it.
    """
    monkeypatch.setattr(checkin, "HOME", tmp_path)
    monkeypatch.setattr(checkin, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(checkin, "LOG_FILE", tmp_path / "checkin.log")
    monkeypatch.setattr(checkin.node_credits, "read_node",
                        lambda name: {"detail": "health /health v0.1.0",
                                        "credits_value": 42, "credits_unit": "points",
                                        "credits_source": "free-windows.json",
                                        "credits_note": "-", "account": "a", "up": True})
    try:
        rc = asyncio.run(checkin.run_tasks(["qoder"]))
        assert rc == 0
        state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
        assert state["qoder"]["na"] is True
        assert state["qoder"]["ok"] is False
        # the credits the node does expose are still recorded
        assert state["qoder"]["available_points"] == 42
        assert "last_success_date" not in state["qoder"]
    finally:
        pass

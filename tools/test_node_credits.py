"""The per-node credits view has to answer for every node, and honestly.

Measured 2026-10-01: the check-in daemon covered 2 of 15 nodes and the picker's
credits annotation never carried a balance, so a node with no balance API would
either be missing from the view or show a zero it had not earned. These tests
pin the two properties that make the view trustworthy: every node in
fleet_probe's table has a row, and a row that cannot prove a number says where
it looked instead of inventing one.
"""
import base64
import http.server
import importlib.util
import json
import os
import re
import socket
import threading

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))

spec = importlib.util.spec_from_file_location("node_credits", os.path.join(HERE, "node_credits.py"))
node_credits = importlib.util.module_from_spec(spec)
spec.loader.exec_module(node_credits)

probe_spec = importlib.util.spec_from_file_location("fleet_probe", os.path.join(HERE, "fleet_probe.py"))
fleet_probe = importlib.util.module_from_spec(probe_spec)
probe_spec.loader.exec_module(fleet_probe)

EVERY_NODE = sorted(set(fleet_probe.PORTS) | set(fleet_probe.GATEWAY))


class _Handler(http.server.BaseHTTPRequestHandler):
    """A stand-in bridge: one body per path, recorded per request."""

    routes = {}

    def do_GET(self):
        body, code = self.routes.get(self.path, (None, 404))
        if body is None:
            self.send_response(code)
            self.end_headers()
            return
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        if self.path == "/":
            # the bridge sets its dashboard session cookie here; the reader has
            # to collect it before /ui/checkin will answer
            self.send_header("Set-Cookie", "workbuddy_bridge_session=test-session; Path=/")
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        body, code = self.routes.get(self.path, (None, 404))
        if body is None:
            self.send_response(code)
            self.end_headers()
            return
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class _Server:
    def __init__(self, routes):
        self.handler = type("H", (_Handler,), {"routes": routes})
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self.handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _dead_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_every_node_in_the_fleet_has_a_row():
    """A node added to fleet_probe cannot be forgotten here."""
    rows = node_credits.read_all()
    names = [row["node"] for row in rows]
    assert set(EVERY_NODE) <= set(names)
    # the two coding plans run their own bridges now (kimi-code on 8802,
    # minimax on 8803), so PLAN_ACCOUNTS overlaps EVERY_NODE: the fleet is
    # the union of the two sets, not the sum -- one name, one row
    assert set(node_credits.PLAN_ACCOUNTS) <= set(names)
    assert len(rows) == len(set(EVERY_NODE) | set(node_credits.PLAN_ACCOUNTS))


def test_every_row_declares_where_its_number_came_from():
    """The source is what separates a reading from a guess."""
    for row in node_credits.read_all():
        assert row["credits_kind"] in node_credits.credits_db()["kinds"] or row["credits_kind"] == "unknown"
        assert row["credits_source"], row["node"]
        assert row["checkin"], row["node"]
        assert row["credits_note"], row["node"]


def test_a_bridge_that_answers_reports_its_account(monkeypatch):
    routes = {
        "/health": ({"ok": True, "version": "0.1.0", "logged_in": True,
                     "ide_account": "aliyun0429921434", "plan": "pro",
                     "models": ["a"]}, 200),
    }
    server = _Server(routes)
    try:
        monkeypatch.setitem(node_credits.PORTS, "qoder", server.port)
        row = node_credits.read_node("qoder")
    finally:
        server.close()
    assert row["up"] is True
    assert row["account"] == "aliyun0429921434"
    assert row["plan"] == "pro"
    assert row["logged_in"] is True
    assert "health" in row["detail"]


def test_a_bridge_that_is_down_says_so_instead_of_zeroing(monkeypatch):
    port = _dead_port()
    monkeypatch.setitem(node_credits.PORTS, "qoder", port)
    row = node_credits.read_node("qoder")
    assert row["up"] is False
    assert row["credits_value"] is None
    assert row["credits_source"]
    assert "不可达" in row["detail"]


def test_zcode_entitlements_become_a_token_quota(monkeypatch):
    routes = {
        "/health": ({"ok": True, "logged_in": True, "captcha": "present"}, 200),
        "/entitlements": ({"glm-5.3-flash": {"grant_units": 100000000,
                                             "plan": "ZCode Trust Build",
                                             "status": "active"}}, 200),
    }
    server = _Server(routes)
    try:
        monkeypatch.setitem(node_credits.PORTS, "zcode", server.port)
        row = node_credits.read_node("zcode")
    finally:
        server.close()
    assert row["credits_value"] == 100000000
    assert row["credits_unit"] == "tokens"
    assert row["credits_source"] == "bridge /entitlements"
    assert "ZCode Trust Build" in row["credits_note"]
    # /health has no plan field for this node, so the entitlement name is the
    # only one there is -- a row that dropped it would show a blank cell next
    # to a hundred million tokens.
    assert row["plan"] == "ZCode Trust Build"


def test_a_plan_that_expires_shows_the_date(monkeypatch):
    """A plan the bridge publishes an expiry for is half an answer without it."""
    routes = {
        "/health": ({"ok": True, "logged_in": True, "edition": "Trae CN",
                     "expires_at_ms": 1791554733096.0, "models": ["a"]}, 200),
    }
    server = _Server(routes)
    try:
        monkeypatch.setitem(node_credits.PORTS, "trae", server.port)
        row = node_credits.read_node("trae")
    finally:
        server.close()
    assert row["plan"] == "Trae CN"
    assert row["credits_note"].endswith("到期")
    assert re.search(r"\d{4}-\d{2}-\d{2}", row["credits_note"])


def test_a_bridge_that_only_names_its_tier_shows_it_as_the_plan(monkeypatch):
    """Gemini answers currentTier.id, not a marketing plan name."""
    routes = {
        "/health": ({"ok": True, "logged_in": True, "tier": "PLUS",
                     "models": ["a"]}, 200),
    }
    server = _Server(routes)
    try:
        monkeypatch.setitem(node_credits.PORTS, "qoder", server.port)
        row = node_credits.read_node("qoder")
    finally:
        server.close()
    assert row["plan"] == "PLUS"


def _checkin_routes(accounts, activity=None):
    # The summary fields stay at the bridge-old values on purpose: the reader
    # derives what is owed from the per-account flags, so a stale count must
    # not be able to hide an account that still needs claiming.
    return {
        "/": ({"bridge": "workbuddy2codex"}, 200),
        "/ui/checkin": ({"ok": True, "activity": activity or {
            "active": True, "theme_name": "Buddy加油站", "daily_credit": 100.0},
            "accounts": accounts, "unclaimed_count": 0, "any_unclaimed": False}, 200),
        "/ui/checkin/claim": ({"ok": True, "claimed_accounts": 1, "claimed_total": 100.0}, 200),
    }


def test_a_claimed_account_reads_as_checked_in(monkeypatch):
    routes = _checkin_routes([{"ref": "r1", "name": "MichaelQiu", "ok": True,
                               "today_checked_in": True, "claimed": True,
                               "credit": 100.0, "streak_days": 2, "message": ""}])
    server = _Server(routes)
    try:
        monkeypatch.setitem(node_credits.PORTS, "workbuddy", server.port)
        row = node_credits.read_node("workbuddy")
    finally:
        server.close()
    assert row["up"] is True
    assert row["account"] == "MichaelQiu"
    assert row["credits_value"] == 100.0
    assert row["credits_unit"] == "credits"
    assert row["checked_in"] is True
    assert "连签 2" in row["checkin"]
    assert row["streak_days"] == 2


def test_an_account_that_cannot_read_its_state_is_not_a_claim(monkeypatch):
    """The overseas bridge reports ok=false with a reason; that is not a claim.

    Measured 2026-10-01: before the backend fix the overseas pool answered
    401 Authorization Required from the CN host, and the row has to say so
    rather than show the day as done.
    """
    routes = _checkin_routes(
        [{"ref": "r1", "name": "overseas-account@example.invalid", "ok": False,
          "today_checked_in": False, "claimed": False, "credit": 0,
          "streak_days": 0, "message": "状态读取失败：401"}],
        activity={"active": False, "theme_name": "Buddy加油站", "daily_credit": 0})
    server = _Server(routes)
    try:
        monkeypatch.setitem(node_credits.PORTS, "workbuddy-gpt", server.port)
        row = node_credits.read_node("workbuddy-gpt")
    finally:
        server.close()
    assert row["checked_in"] is False
    assert row["logged_in"] is False
    assert "401" in row["checkin"]
    assert "活动当前不可用" in row["credits_note"]


def test_an_unclaimed_account_is_reported_as_owed(monkeypatch):
    routes = _checkin_routes([{"ref": "r1", "name": "a", "ok": True,
                               "today_checked_in": False, "claimed": False,
                               "credit": 100.0, "streak_days": 0, "message": ""}])
    server = _Server(routes)
    try:
        monkeypatch.setitem(node_credits.PORTS, "workbuddy", server.port)
        row = node_credits.read_node("workbuddy")
    finally:
        server.close()
    assert row["checked_in"] is False
    assert "待领取" in row["checkin"]


def test_a_gateway_node_reports_the_gateway_not_a_bridge(monkeypatch):
    """stepfun/tokendance have no local bridge; the gateway is the whole story."""
    monkeypatch.setattr(node_credits, "GATEWAY_PORT", _dead_port())
    row = node_credits.read_node("stepfun")
    assert row["up"] is False
    assert "网关" in row["detail"]
    assert "官方控制台" in row["credits_note"]
    assert row["checkin"] == node_credits.NO_CHECKIN_NOTE


def test_the_loopback_read_survives_a_proxy_environment(monkeypatch):
    """Regression: HTTP_PROXY must not swallow the loopback bridge calls.

    The reader builds its own opener with an empty ProxyHandler, so a proxy
    that is set in the environment -- and dead -- cannot make a healthy bridge
    read as down.
    """
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:%d" % _dead_port())
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:%d" % _dead_port())
    routes = {"/health": ({"ok": True, "logged_in": True, "account": "x"}, 200)}
    server = _Server(routes)
    try:
        monkeypatch.setitem(node_credits.PORTS, "qoder", server.port)
        row = node_credits.read_node("qoder")
        assert row["up"] is True
        assert row["account"] == "x"
    finally:
        server.close()


def test_credits_kinds_come_from_the_database_not_a_second_copy():
    """The kind is what free_models.py already defines; no local re-definition."""
    db = node_credits.credits_db()
    assert "providers" in db and "kinds" in db
    assert node_credits.credits_kind("workbuddy") == "client"
    assert node_credits.credits_kind("trae") == "limit"
    assert node_credits.credits_kind("no-such-node") == "unknown"


SUB = "9f7447af-ae07-4209-a1b6-f3cdba17927e"


def _jwt(claims):
    """An unsigned JWT carrying exactly the payload the test wants read back."""
    def seg(obj):
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    return "%s.%s.sig" % (seg({"alg": "none"}), seg(claims))


def test_a_bridge_that_keeps_its_login_in_a_file_reports_it(
        monkeypatch, tmp_path):
    """A bridge that names no account is still signed in; say whose.

    Measured 2026-10-01: lingxi answers logged_in=true on /health and no
    account at all, so the panel drew a dash beside a live session. The
    identity the vendor issued is already on disk in the bridge's own
    auth.json, so the row reads that instead of asking the vendor again --
    which is the only option for vendors like gemini that answer 403 on the
    account level today.
    """
    (tmp_path / "auth.json").write_text(json.dumps(
        {"token": _jwt({"sub": SUB, "name": ""}), "name": ""}),
        encoding="utf-8")
    monkeypatch.setenv("LINGXI_HOME", str(tmp_path))
    routes = {"/health": ({"ok": True, "version": "0.1.0", "logged_in": True},
                          200)}
    server = _Server(routes)
    try:
        monkeypatch.setitem(node_credits.PORTS, "lingxi", server.port)
        row = node_credits.read_node("lingxi")
    finally:
        server.close()
    assert row["up"] is True
    assert row["account"] == SUB
    assert "account 本机凭据" in row["detail"]


def test_the_display_name_wins_over_the_subject_id(monkeypatch, tmp_path):
    """The vendor's own UI shows a name, so the panel shows a name.

    xhx writes both a display name and a subject id into auth.json; the name
    is what a reader recognizes, and the id only fills in when a vendor
    issues no name at all -- the case the lingxi test above pins.
    """
    (tmp_path / "auth.json").write_text(json.dumps(
        {"access_token": _jwt({"name": "RaccoonJoshua", "sub": "12345"}),
         "name": "RaccoonJoshua"}), encoding="utf-8")
    monkeypatch.setenv("BOX_AGENT_CONFIG_DIR", str(tmp_path))
    # the balance reader calls upstream; this row is about the account
    monkeypatch.setattr(node_credits, "xhx_points",
                        lambda: {"ok": False, "detail": "test"})
    routes = {"/health": ({"ok": True, "version": "0.1.0", "logged_in": True,
                           "models": ["a"]}, 200)}
    server = _Server(routes)
    try:
        monkeypatch.setitem(node_credits.PORTS, "xhx", server.port)
        row = node_credits.read_node("xhx")
    finally:
        server.close()
    assert row["account"] == "RaccoonJoshua"


def test_a_machine_that_never_logged_in_keeps_the_dash(monkeypatch, tmp_path):
    """No credential file means no identity -- a dash, never an invented one.

    The fallback reads the login this machine already has, so a machine with
    none must stay blank rather than borrow the name out of whoever else's
    ~/.box-agent/config happens to sit on the box running the tests.
    """
    monkeypatch.setenv("BOX_AGENT_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(node_credits, "xhx_points",
                        lambda: {"ok": False, "detail": "test"})
    routes = {"/health": ({"ok": True, "logged_in": True, "models": []}, 200)}
    server = _Server(routes)
    try:
        monkeypatch.setitem(node_credits.PORTS, "xhx", server.port)
        row = node_credits.read_node("xhx")
    finally:
        server.close()
    assert row["up"] is True
    assert row["account"] == ""
    assert "account 本机凭据" not in row["detail"]


def test_a_node_that_keeps_no_credential_file_has_no_fallback():
    """Only the two JWT-on-disk bridges have a local identity to read."""
    assert node_credits.local_account("trae") == ""


# ---------------- the bridge account pool ----------------

def _pool_health(accounts):
    """What the kimi/minimax bridge answers on /health with a pool."""
    return {"ok": True, "account_pool": {"accounts": accounts,
                                         "count": len(accounts)}}


def _account(tail, *, points, state="ready", primary=False, unit="credits",
           brand="kimi"):
    """One status() row of bridges/plan_key_pool.py, as the bridge publishes."""
    return {"name": "%s\u2026%s" % (brand, tail), "key_tail": tail,
            "state": state,
            "primary": primary, "points": points, "points_unit": unit,
            "source": "admin"}


class _PoolBridge:
    """PLAN_ACCOUNTS pointed at a fake bridge on the ports under test."""

    def __init__(self, monkeypatch, health):
        self.server = _Server({"/health": (health, 200)})
        monkeypatch.setattr(node_credits, "PLAN_ACCOUNTS",
                            {name: {**spec, "port": self.server.port}
                             for name, spec in node_credits.PLAN_ACCOUNTS.items()})

    def read(self, name):
        return node_credits._pool_accounts_row(name, node_credits._row(name))

    def close(self):
        self.server.close()


def test_the_bridge_pool_fills_the_credits_row(monkeypatch):
    """The pool is what the bridge burns, so it is the honest source."""
    bridge = _PoolBridge(monkeypatch, _pool_health([
        _account("irst", points=6420, primary=True),
        _account("cond", points=300, state="cooling"),
    ]))
    try:
        row, from_pool = bridge.read("kimi-code")
    finally:
        bridge.close()

    assert from_pool is True
    assert row["credits_source"] == "bridge /health account_pool"
    assert row["credits_value"] == 6420, "the primary account is the headline"
    assert row["credits_unit"] == "credits"
    assert row["account"] == "kimi\u2026irst"
    assert row["up"] is True
    assert row["logged_in"] is True
    # one line the panel can put under the account name
    assert row["credits_accounts"] == "kimi\u2026irst 6,420 / kimi\u2026cond 300"
    note = row["credits_note"]
    assert "2 account(s)" in note
    assert "6,720 points total" in note
    assert "kimi\u2026cond 300 (cooling)" in note


def test_a_pool_without_accounts_falls_back_to_the_key(monkeypatch):
    """A bridge up with an empty pool has nothing to say about accounts."""
    bridge = _PoolBridge(monkeypatch, _pool_health([]))
    try:
        row, from_pool = bridge.read("kimi-code")
    finally:
        bridge.close()

    assert from_pool is False


def test_a_dead_bridge_port_falls_back(monkeypatch):
    """No bridge running: the key in the environment is still the fallback."""
    monkeypatch.setattr(node_credits, "PLAN_ACCOUNTS",
                        {name: {**spec, "port": _dead_port()}
                         for name, spec in node_credits.PLAN_ACCOUNTS.items()})

    _row, from_pool = node_credits._pool_accounts_row(
        "kimi-code", node_credits._row("kimi-code"))

    assert from_pool is False


def test_an_account_without_a_published_balance_says_so(monkeypatch):
    """A pay-as-you-go key has no Token Plan behind it: not a zero."""
    bridge = _PoolBridge(monkeypatch, _pool_health([
        _account("real", points=None, primary=True, unit="", brand="minimax"),
    ]))
    try:
        row, from_pool = bridge.read("minimax")
    finally:
        bridge.close()

    assert from_pool is True
    assert row["credits_value"] is None
    assert row["credits_unit"] == ""
    # a single account is not a pool: no one-line roster was earned
    assert row["credits_accounts"] is None
    assert "the balance route only accepts a subscription Key" in row["credits_note"]
    assert "pay-as-you-go" in row["credits_note"]
    assert "1 account(s)" in row["credits_note"]

"""A blocked upstream must cost one bounded request, not a multi-minute hang.

Measured 2026-09-29 with the pre-fix code:

  * gemini:  call_a(180s) then call_b(180s) -> worst case six minutes before a
    502 was even attempted, while the client had given up at ~70s. All that
    survived in the log was a BrokenPipeError from writing the 502 into a dead
    pipe, which made the health check report BRIDGE_DOWN for a bridge that was
    merely waiting on an unreachable upstream.
  * antigravity:  call_upstream() ran with timeout=180 for every
    model_variants x IDE_TYPES combination, and get_access() added a 30s
    refresh per OAuth client on top. Same outcome: minutes of silence, then a
    traceback that named the pipe error instead of the upstream.

Both bridges now carry one CHAT_BUDGET deadline and charge each attempt only
the time that is left. These tests pin the arithmetic and the fallback chain,
and pin that writing into a hung-up client is not an error worth a traceback.
"""
import importlib.util
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

BRIDGES = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, "bridges"))
sys.path.insert(0, BRIDGES)


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(BRIDGES, rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gemini = _load("gemini_bridge", os.path.join("gemini", "gemini_bridge.py"))
antigravity = _load("antigravity_bridge",
                    os.path.join("antigravity", "antigravity_bridge.py"))


class _Bridge:
    """One module's real http.server handler, listening on an ephemeral port."""

    def __init__(self, mod):
        self.mod = mod
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), mod.H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def post(self, payload, timeout=60):
        req = urllib.request.Request(
            "http://127.0.0.1:%d/v1/chat/completions" % self.port,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        started = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read() or b"{}"), time.monotonic() - started
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}"), time.monotonic() - started

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


# ---------------------------------------------------------------- gemini

GEMINI_PAYLOAD = {"model": "gemini-3-pro-preview",
                  "messages": [{"role": "user", "content": "ping"}]}


def test_gemini_fallback_channel_gets_the_remaining_budget(monkeypatch):
    """call_b used to start with the 180s default, i.e. a second full hang."""
    monkeypatch.setattr(gemini, "CHAT_BUDGET", 10.0)
    seen = []

    def fake_a(model, msgs, stream, timeout=180, deadline=None):
        seen.append(("code-assist", deadline and deadline - time.time()))
        time.sleep(1.0)
        raise gemini.UpstreamError("codeassist error: HTTP 503")

    def fake_b(prompt, timeout=180):
        seen.append(("gemini-web", timeout))
        return "pong"

    monkeypatch.setattr(gemini, "call_a", fake_a)
    monkeypatch.setattr(gemini, "call_b", fake_b)
    bridge = _Bridge(gemini)
    try:
        code, body, elapsed = bridge.post(GEMINI_PAYLOAD)
    finally:
        bridge.close()

    assert code == 200
    assert body["choices"][0]["message"]["content"] == "pong"
    assert [name for name, _ in seen] == ["code-assist", "gemini-web"]
    # channel A still owns the whole budget ...
    assert seen[0][1] == pytest.approx(10.0)
    # ... and channel B gets the ~9s that are left of it, not another 180s.
    assert 8.0 < seen[1][1] <= 9.0
    assert elapsed < 2.5


def test_gemini_an_overrun_still_leaves_the_fallback_a_floor(monkeypatch):
    """A connect that outlives its own timeout must not starve channel B."""
    monkeypatch.setattr(gemini, "CHAT_BUDGET", 0.5)
    seen = []

    def fake_a(model, msgs, stream, timeout=180):
        time.sleep(0.7)                     # overshoots the budget on purpose
        raise gemini.UpstreamError("codeassist error: timed out")

    def fake_b(prompt, timeout=180):
        seen.append(timeout)
        return "pong"

    monkeypatch.setattr(gemini, "call_a", fake_a)
    monkeypatch.setattr(gemini, "call_b", fake_b)
    bridge = _Bridge(gemini)
    try:
        code, body, _ = bridge.post(GEMINI_PAYLOAD)
    finally:
        bridge.close()

    assert code == 200
    assert body["choices"][0]["message"]["content"] == "pong"
    # nothing is left, so B gets exactly the floor -- never a second full timeout
    assert seen == [pytest.approx(gemini.FALLBACK_FLOOR)]
    assert gemini.FALLBACK_FLOOR == 1.0


def test_gemini_both_channels_failing_answers_502_not_a_hang(monkeypatch):
    monkeypatch.setattr(gemini, "CHAT_BUDGET", 1.0)
    seen = []

    def burn_budget(deadline):
        """An honoured upstream hangs for exactly the time it was handed."""
        left = max(0.0, deadline - time.time())
        seen.append(left)
        time.sleep(left)
        raise gemini.UpstreamError("timed out")

    def fake_a(model, msgs, stream, timeout=180, deadline=None):
        burn_budget(deadline)

    def fake_b(prompt, timeout=180):
        burn_budget(time.time() + timeout)

    monkeypatch.setattr(gemini, "call_a", fake_a)
    monkeypatch.setattr(gemini, "call_b", fake_b)
    bridge = _Bridge(gemini)
    try:
        code, body, elapsed = bridge.post(GEMINI_PAYLOAD)
    finally:
        bridge.close()

    assert code == 502
    assert set(body["error"]["message"]) == {"code_assist", "web"}
    # the whole budget for A, the floor for B -- where it used to be 180 + 180
    assert len(seen) == 2
    assert seen[0] == pytest.approx(1.0, abs=0.1)
    assert seen[1] == pytest.approx(gemini.FALLBACK_FLOOR, abs=0.1)
    assert elapsed < 3.0


def test_gemini_web_channel_page_fetch_is_charged_to_the_budget(monkeypatch):
    """The /app page fetch had its own hardcoded 30s timeout.

    It sat outside CHAT_BUDGET, so a blocked upstream cost 60s (channel A) + 30s
    (page fetch) + the web call itself -- more than any budget could promise,
    and past the 70s the health probe is willing to wait.
    """
    seen = []

    def fake_http_json(url, payload, headers=None, method='POST', timeout=90):
        seen.append((url, method, timeout))
        raise gemini.UpstreamError("URLError: timed out")

    monkeypatch.setattr(gemini, "web_cookies", lambda: "__Secure-1PSID=fake")
    monkeypatch.setattr(gemini, "http_json", fake_http_json)

    with pytest.raises(gemini.UpstreamError):
        gemini.call_b("ping", timeout=2.0)

    url, method, timeout = seen[0]
    assert method == "GET" and url.endswith("/app")
    assert timeout == pytest.approx(2.0)      # the remaining budget, not 30


def test_gemini_web_channel_page_fetch_keeps_its_30s_ceiling(monkeypatch):
    """A generous budget must not turn the page fetch into a long hang either."""
    seen = []

    def fake_http_json(url, payload, headers=None, method='POST', timeout=90):
        seen.append(timeout)
        raise gemini.UpstreamError("URLError: timed out")

    monkeypatch.setattr(gemini, "web_cookies", lambda: "__Secure-1PSID=fake")
    monkeypatch.setattr(gemini, "http_json", fake_http_json)

    with pytest.raises(gemini.UpstreamError):
        gemini.call_b("ping", timeout=180.0)

    assert seen[0] == pytest.approx(30.0)


# The health check caught a second, deeper leak in the same bridge: do_POST
# bounded call_a/call_b, but the token refresh and loadCodeAssist *inside* call_a
# each carried their own 30s timeout, so a cold bridge still spent 60 + 30 + 30.
def _no_token_file(monkeypatch):
    monkeypatch.setattr(gemini, "TOKEN_FILE", "/nonexistent/jetski-token")
    monkeypatch.setattr(gemini, "read_token_file", lambda: {"token": {"refresh_token": "1//fake"}})
    monkeypatch.setattr(gemini, "write_token_file", lambda d: None)


def test_gemini_a_slow_token_refresh_is_charged_to_the_budget(monkeypatch):
    """do_refresh tried every client for 30s each, outside any budget."""
    _no_token_file(monkeypatch)
    monkeypatch.setattr(gemini, "CLIENT_CANDIDATES", [("cid-1", "sec-1"), ("cid-2", "sec-2")])
    gemini.ST.update({"at": None, "exp": 0.0, "project": None})
    seen = []

    def fake_http_json(url, payload, headers=None, method='POST', timeout=90):
        seen.append(timeout)
        time.sleep(timeout)                     # honour the timeout it was handed
        raise gemini.UpstreamError("URLError: timed out")

    monkeypatch.setattr(gemini, "http_json", fake_http_json)

    started = time.monotonic()
    with pytest.raises(gemini.UpstreamError):
        gemini.call_a("gemini-3-pro-preview", [{"role": "user", "content": "ping"}],
                      False, deadline=time.time() + 1.0)
    elapsed = time.monotonic() - started

    assert seen, "do_refresh never tried a client"
    assert seen[0] == pytest.approx(1.0)        # the remaining budget, not 30
    assert len(seen) == 1                       # client 2 was never tried
    assert elapsed < 2.0


def test_gemini_load_code_assist_is_charged_to_the_budget(monkeypatch):
    _no_token_file(monkeypatch)
    monkeypatch.setattr(gemini, "ST",
                        {"at": "fresh", "exp": time.time() + 3600, "project": None})
    seen = []

    def fake_http_json(url, payload, headers=None, method='POST', timeout=90):
        seen.append(timeout)
        raise gemini.UpstreamError("URLError: timed out")

    monkeypatch.setattr(gemini, "http_json", fake_http_json)

    with pytest.raises(gemini.UpstreamError):
        gemini.load_code_assist(time.time() + 2.0)

    assert seen == [pytest.approx(2.0, abs=0.2)]   # the remaining budget, not 30


# ---------------------------------------------------------- antigravity

ANTIGRAVITY_PAYLOAD = {"model": "claude-opus-4-8@default",
                       "messages": [{"role": "user", "content": "ping"}]}


def test_antigravity_a_blocked_upstream_abandons_the_chain(monkeypatch):
    """An unreachable upstream used to walk every variant and IDE type too."""
    monkeypatch.setattr(antigravity, "CHAT_BUDGET", 0.5)
    attempts = []

    def fake_upstream(model, msgs, stream, timeout=180, ide=None):
        attempts.append((timeout, ide))
        time.sleep(0.6)                     # blows past the deadline
        raise antigravity.UpstreamError("HTTP 404: model not found")

    monkeypatch.setattr(antigravity, "call_upstream", fake_upstream)
    bridge = _Bridge(antigravity)
    try:
        code, body, elapsed = bridge.post(ANTIGRAVITY_PAYLOAD)
    finally:
        bridge.close()

    assert code == 502
    assert "404" in body["error"]["message"]
    # model_variants() gives two ids and IDE_TYPES one more, i.e. three 180s
    # timeouts in the old code. The deadline must cut the chain to one.
    assert len(attempts) == 1
    assert attempts[0][1] is None
    assert elapsed < 1.5


def test_antigravity_later_attempts_are_charged_the_time_left(monkeypatch):
    """No attempt after the first may be handed the 180s default again."""
    monkeypatch.setattr(antigravity, "CHAT_BUDGET", 2.0)
    seen = []

    def fake_upstream(model, msgs, stream, timeout=180, ide=None):
        seen.append(timeout)
        time.sleep(0.3)
        raise antigravity.UpstreamError("HTTP 404: model not found")

    monkeypatch.setattr(antigravity, "call_upstream", fake_upstream)
    bridge = _Bridge(antigravity)
    try:
        code, _, _ = bridge.post(ANTIGRAVITY_PAYLOAD)
    finally:
        bridge.close()

    assert code == 502
    # 2 variants + 1 IDE type, all fast enough to fit inside the budget
    assert len(seen) == len(antigravity.model_variants("x")) + len(antigravity.IDE_TYPES) - 1
    assert seen[0] == pytest.approx(2.0, abs=0.05)
    assert seen == sorted(seen, reverse=True)
    assert all(antigravity.FALLBACK_FLOOR <= t <= 2.0 for t in seen)
    assert all(t < 2.0 for t in seen[1:])


def test_antigravity_the_ide_fallback_still_succeeds_inside_the_budget(monkeypatch):
    """The deadline must not cost the bridge its working fallback path."""
    monkeypatch.setattr(antigravity, "CHAT_BUDGET", 5.0)
    tried = []

    def fake_upstream(model, msgs, stream, timeout=180, ide=None):
        tried.append(ide)
        if ide != "GEMINI_CLI":
            raise antigravity.UpstreamError("HTTP 404: unknown model")
        return "pong"

    monkeypatch.setattr(antigravity, "call_upstream", fake_upstream)
    bridge = _Bridge(antigravity)
    try:
        code, body, elapsed = bridge.post(ANTIGRAVITY_PAYLOAD)
    finally:
        bridge.close()

    assert code == 200
    assert body["choices"][0]["message"]["content"] == "pong"
    assert body["channel"] == "antigravity-code-assist"
    # the variant loop leaves ide unset, then the IDE type switches once
    assert tried[:-1] == [None, None]
    assert tried[-1] == "GEMINI_CLI"
    assert elapsed < 4.0


# ------------------------------------------------- both http.server shells

# ------------------------------------------------- both http.server shells

class _Sink:
    """A fake handler socket: records every write, or refuses them all.

    BaseHTTPRequestHandler.end_headers() writes the header block through
    self.wfile as well, so the refusing variant models the real failure point
    -- that is where the BrokenPipeError surfaced, not the body write.
    """

    def __init__(self, exc=None):
        self.exc = exc
        self.code = None
        self.chunks = []
        self._headers = []
        self.wfile = self

    def write(self, b):
        if self.exc is not None:
            raise self.exc
        self.chunks.append(b)

    def flush(self):
        pass

    def send_response(self, code):
        self.code = code
        self._headers.append(b"HTTP/1.1 200 OK\r\n")

    def send_header(self, name, value):
        self._headers.append(("%s: %s\r\n" % (name, value)).encode())

    def end_headers(self):
        self._headers.append(b"\r\n")
        self.write(b"".join(self._headers))


@pytest.mark.parametrize("exc", [BrokenPipeError(32, "Broken pipe"),
                                 ConnectionResetError(54, "Connection reset by peer")])
@pytest.mark.parametrize("which", ["gemini", "antigravity"])
def test_send_swallows_a_client_that_hung_up(which, exc):
    """The 502 has nowhere to go; a traceback only buries the upstream error."""
    mod = gemini if which == "gemini" else antigravity
    stub = _Sink(exc)
    mod.H._send(stub, 200, json.dumps({"ok": True}))     # must not raise
    assert stub.code == 200


def test_send_still_writes_for_a_client_that_is_listening():
    """The guard must not swallow a reply that could still be delivered."""
    stub = _Sink()
    gemini.H._send(stub, 200, json.dumps({"ok": True}))
    payload = b"".join(stub.chunks)
    assert b"Content-Length" in payload
    assert json.loads(payload.split(b"\r\n\r\n", 1)[1]) == {"ok": True}


# ------------------------------------------- socket-level deadline (both bridges)

@pytest.mark.parametrize("which", ["gemini", "antigravity"])
def test_every_address_is_charged_to_the_deadline(which):
    """One blocked urlopen() must not cost N x timeout.

    cloudcode-pa.googleapis.com resolves to 16 addresses, 8 of them IPv6 that
    this network blackholes. A 20s timeout therefore cost 40s on
    oauth2.googleapis.com's two addresses; capping only the first connect left
    the rest of the walk unbounded.
    """
    mod = gemini if which == "gemini" else antigravity
    blackhole = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.255.255.1", 443)),
                 (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.255.255.2", 443))]
    seen = []

    def fake_connect(self, address):
        seen.append(self.gettimeout())
        time.sleep(self.gettimeout())          # a blackhole burns it all
        raise OSError("timed out")

    orig_getaddrinfo, orig_connect = socket.getaddrinfo, socket.socket.connect
    socket.getaddrinfo = lambda host, port, *a, **k: blackhole
    socket.socket.connect = fake_connect
    try:
        mod._arm_deadline(time.time() + 1.0)
        started = time.monotonic()
        try:
            with pytest.raises(OSError):
                mod._budgeted_create_connection(("example.invalid", 443), 30.0)
        finally:
            elapsed = time.monotonic() - started
            mod._disarm_deadline()
    finally:
        socket.getaddrinfo, socket.socket.connect = orig_getaddrinfo, orig_connect

    # The first address alone can burn the whole budget; what matters is that
    # the walk stops there instead of handing the next address a fresh 30s.
    assert seen[0] == pytest.approx(1.0, abs=0.05)
    assert seen == sorted(seen, reverse=True)
    assert all(t <= 1.0 for t in seen)
    assert elapsed <= 1.2


@pytest.mark.parametrize("which", ["gemini", "antigravity"])
def test_the_deadline_does_not_break_a_reachable_connect(which):
    """Walking the addresses by hand must not cost the bridge its sockets."""
    mod = gemini if which == "gemini" else antigravity
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    mod._arm_deadline(time.time() + 10.0)
    try:
        sock = mod._budgeted_create_connection(("127.0.0.1", port), 5.0)
    finally:
        mod._disarm_deadline()
    try:
        assert sock.getpeername()[1] == port
    finally:
        sock.close()
        listener.close()


def test_without_a_deadline_it_delegates_to_the_stdlib():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        sock = gemini._budgeted_create_connection(("127.0.0.1", port), 5.0)
    finally:
        listener.close()
    sock.close()
    assert getattr(gemini._tls, "deadline", None) is None


def test_antigravity_the_deadline_is_disarmed_when_call_model_returns(monkeypatch):
    """A thread-local left armed would silently cap every later connect."""
    monkeypatch.setattr(antigravity, "CHAT_BUDGET", 1.0)

    def fake_upstream(model, msgs, stream, timeout=180, ide=None):
        raise antigravity.UpstreamError("URLError: timed out")

    monkeypatch.setattr(antigravity, "call_upstream", fake_upstream)

    with pytest.raises(antigravity.UpstreamError):
        antigravity.call_model("claude-opus-4-8@default", [], False)

    assert getattr(antigravity._tls, "deadline", None) is None

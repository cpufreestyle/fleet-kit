"""The prover must grade the streaming path, not only the buffered one.

Measured 2026-09-29: verify_real_calls.py always posted "stream": False, so it
could not see that lingxi / xhx / codely answered HTTP 500 on every streaming
call -- they constructed a StreamingResponse that was never imported (NameError
in the handler) while /health, /v1/models and the arithmetic chat all reported
REAL. Codex streams by design, so those three bridges were unusable in practice
and every automated check said otherwise.

apply_stream_probe closes that gap: the REAL path is re-probed once with
stream=true and downgraded to STREAM_BROKEN when no SSE block arrives.
"""
import json
import os
import re
import urllib.error

import pytest
from test_bridge_loader import HERE, load_tool as _load





vrc = _load("verify_real_calls", "verify_real_calls.py")
status_ui = _load("status_ui", "status_ui.py")

SSE = [b": keep-alive\n",
       b"event: message\n",
       b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n',
       b"data: [DONE]\n"]


class _FakeResp(object):
    def __init__(self, status=200, lines=(), ctype="text/event-stream"):
        self.status = status
        self.headers = {"Content-Type": ctype}
        self._lines = list(lines)

    def readline(self):
        return self._lines.pop(0) if self._lines else b""

    def read(self):
        return b"".join(self._lines)


def _http_error(code, body):
    """An HTTPError whose read() yields body, without touching a socket."""
    e = urllib.error.HTTPError.__new__(urllib.error.HTTPError)
    e.code = code
    e._body = body.encode() if isinstance(body, str) else body
    e.read = lambda: e._body
    return e


def _patch(monkeypatch, responder):
    def fake_open(req, timeout=None):
        seen.append((req, timeout))
        return responder(req)
    seen = []
    monkeypatch.setattr(vrc.NO_PROXY, "open", fake_open)
    return seen


def _real_row():
    return dict(name="xhx", port=8793, model="xhx/raccoon-19b265",
                verdict="REAL", note="运算正确(=真实推理) nonce=ok")


def test_verify_one_downgrades_a_real_chat_with_a_dead_stream(monkeypatch):
    monkeypatch.setattr(vrc, "get_models", lambda port, key: ["xhx/raccoon-19b265"])
    monkeypatch.setattr(vrc, "make_probe", lambda: ("prompt", 100, 23, "AB12"))
    monkeypatch.setattr(vrc, "chat", lambda port, model, key, content, max_tokens=2048: dict(
        code=200, secs=1.0, text="AB12 123", rmodel=model, err="", usage={}))
    monkeypatch.setattr(vrc, "stream_probe", lambda port, model, key: dict(
        code=500, secs=0.3, ctype="", data="",
        err="NameError: name 'StreamingResponse' is not defined"))
    row = vrc.verify_one("xhx", "xhx2codex", 6, "xhx/raccoon-19b265",
                         "XHX2CODEX_KEY", 8787, {"xhx2codex": "sk-x"})
    assert row["verdict"] == "STREAM_BROKEN"
    assert row["stream_ok"] is False
    assert "HTTP 500" in row["note"]


def test_verify_one_keeps_real_when_the_stream_answers(monkeypatch):
    monkeypatch.setattr(vrc, "get_models", lambda port, key: ["xhx/raccoon-19b265"])
    monkeypatch.setattr(vrc, "make_probe", lambda: ("prompt", 100, 23, "AB12"))
    monkeypatch.setattr(vrc, "chat", lambda port, model, key, content, max_tokens=2048: dict(
        code=200, secs=1.0, text="AB12 123", rmodel=model, err="", usage={}))
    monkeypatch.setattr(vrc, "stream_probe", lambda port, model, key: dict(
        code=200, secs=0.4, ctype="text/event-stream", data="data: {}", err=""))
    row = vrc.verify_one("xhx", "xhx2codex", 6, "xhx/raccoon-19b265",
                         "XHX2CODEX_KEY", 8787, {"xhx2codex": "sk-x"})
    assert row["verdict"] == "REAL"
    assert row["stream_ok"] is True


def test_real_row_downgrades_when_stream_true_500s(monkeypatch):
    seen = _patch(monkeypatch, lambda req: (_ for _ in ()).throw(
        _http_error(500, "NameError: name 'StreamingResponse' is not defined")))
    row = _real_row()
    out = vrc.apply_stream_probe(row, 8793, "xhx/raccoon-19b265", "sk-x")
    assert out is row
    assert out["verdict"] == "STREAM_BROKEN"
    assert out["stream_ok"] is False
    assert "HTTP 500" in out["note"] and "stream=true" in out["note"]
    assert out["stream"]["code"] == 500
    assert seen, "the probe must actually issue a request"


def test_real_row_survives_when_sse_arrives(monkeypatch):
    seen = _patch(monkeypatch, lambda req: _FakeResp(200, SSE))
    row = _real_row()
    out = vrc.apply_stream_probe(row, 8793, "xhx/raccoon-19b265", "sk-x")
    assert out["verdict"] == "REAL"
    assert out["stream_ok"] is True
    assert out["stream"]["data"].startswith("data:")
    assert "assistant" in out["stream"]["data"]
    assert seen


def test_body_without_data_block_is_broken(monkeypatch):
    _patch(monkeypatch, lambda req: _FakeResp(200, [b": ping\n", b"\n"]))
    row = _real_row()
    out = vrc.apply_stream_probe(row, 8793, "xhx/raccoon-19b265", "sk-x")
    assert out["verdict"] == "STREAM_BROKEN"
    assert out["stream_ok"] is False
    assert "stream=true" in out["note"] and "no data: block" in out["note"]


def test_probe_really_asks_for_a_stream(monkeypatch):
    seen = _patch(monkeypatch, lambda req: _FakeResp(200, SSE))
    vrc.stream_probe(8793, "xhx/raccoon-19b265", "sk-x")
    req, timeout = seen[0]
    payload = json.loads(req.data.decode())
    assert payload["stream"] is True
    assert payload["max_tokens"] == 32
    assert req.headers["Authorization"] == "Bearer sk-x"
    # SSE must not be judged on the 70s non-stream chat timeout.
    assert timeout == vrc.STREAM_PROBE_TIMEOUT


# Every verdict verify_real_calls can emit; a name missing from the maps renders
# as 'idle' and vanishes from the panel.
VERDICTS = ("REAL", "STREAM_BROKEN", "ECHO/MIRROR", "CANNED/MOCK", "UNCLEAR",
            "CHANNEL_BLOCKED", "PLAN_BLOCKED", "AUTH_EXPIRED", "UPSTREAM_DOWN",
            "BRIDGE_DOWN", "GATE")


def test_every_verdict_the_ui_can_draw_is_ranked_and_coloured():
    # A verdict the maps miss renders as 'idle' and silently disappears.
    for v in VERDICTS:
        assert v in status_ui.VERDICT_RANK, v
        assert v in status_ui.VERDICT_KIND, v
    assert status_ui.VERDICT_KIND["STREAM_BROKEN"] == "bad"


def test_the_page_js_knows_the_same_verdicts_as_the_server():
    jsmap = status_ui.PAGE.split("var VF_KIND={")[1].split("};")[0]
    for v in VERDICTS:
        assert re.search(re.escape(v) + r"['\"]?\s*:", jsmap), v
    assert "STREAM_BROKEN:'bad'" in jsmap

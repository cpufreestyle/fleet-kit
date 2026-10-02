"""The code-assist chat payload, and the shape of a failure envelope.

Measured 2026-09-29 against cloudcode-pa.googleapis.com with a real token:

  * POST v1internal:loadCodeAssist  {metadata: {...}}  -> 200
  * POST v1internal:generateContent {model, request, metadata}
        -> 400 INVALID_ARGUMENT: Invalid JSON payload received. Unknown
           name metadata: Cannot find field.
  * POST v1internal:generateContent {model, request}
        -> 403 VALIDATION_REQUIRED: Verify your account to continue.

The antigravity bridge put the metadata field in every generateContent
body, so every chat request was rejected before Google looked at the
model, while /health and the model list stayed green. The gemini bridge
already built that body correctly, which is what made the difference
visible. The IDE identity belongs to loadCodeAssist alone.

These tests pin three things: the chat body carries no metadata, a named
IDE still re-resolves the project under that identity instead of reusing
the cached one, and a gemini 502 hands the client a string message with
the per-channel breakdown one level down.
"""

import importlib.util
import inspect
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer


BRIDGES = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, "bridges"))
sys.path.insert(0, BRIDGES)


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(BRIDGES, rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


antigravity = _load("antigravity_bridge",
                    os.path.join("antigravity", "antigravity_bridge.py"))
gemini = _load("gemini_bridge", os.path.join("gemini", "gemini_bridge.py"))

MSGS = [{"role": "user", "content": "ping"}]
CODECASSIST_OK = json.dumps(
    {"candidates": [{"content": {"parts": [{"text": "pong"}]}}]})
IDE_META = {"ideType": "ANTIGRAVITY", "pluginType": "GEMINI",
            "platform": "PLATFORM_UNSPECIFIED"}


class _Bridge:
    """One module real http.server handler on an ephemeral port."""

    def __init__(self, mod):
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), mod.H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def post(self, payload, timeout=30):
        req = urllib.request.Request(
            "http://127.0.0.1:%d/v1/chat/completions" % self.port,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def _fresh_antigravity(monkeypatch, calls):
    """Record every upstream call and answer both endpoints, no network."""

    def fake_http_json(url, payload, headers=None, method="POST", timeout=90):
        calls.append((url, payload))
        if url.endswith("loadCodeAssist"):
            return json.dumps({"cloudaicompanionProject": "proj-1",
                               "currentTier": {"id": "free"}}), {}
        if url.endswith("generateContent"):
            return CODECASSIST_OK, {}
        raise AssertionError("unexpected url: " + url)

    monkeypatch.setattr(antigravity, "http_json", fake_http_json)
    monkeypatch.setattr(antigravity, "get_access", lambda: "AT")
    monkeypatch.setitem(antigravity.ST, "project", None)
    monkeypatch.setitem(antigravity.ST, "ide", None)


def test_the_generate_content_body_carries_no_metadata(monkeypatch):
    """Google rejects a metadata field on generateContent with a 400."""
    calls = []
    _fresh_antigravity(monkeypatch, calls)

    out = antigravity.call_upstream("claude-haiku-4-5@default", MSGS, False)

    assert out == "pong"
    loads = [p for u, p in calls if u.endswith("loadCodeAssist")]
    chats = [p for u, p in calls if u.endswith("generateContent")]
    assert len(loads) == len(chats) == 1
    # the IDE identity is applied on loadCodeAssist ...
    assert loads[0] == {"metadata": IDE_META}
    # ... and nowhere else: an unknown name there is a 400, not a warning.
    assert "metadata" not in chats[0]
    assert chats[0]["model"] == "claude-haiku-4-5@default"
    assert set(chats[0]["request"]) == {"contents", "generationConfig"}
    assert chats[0]["project"] == "proj-1"


def test_a_named_ide_still_re_resolves_the_project(monkeypatch):
    """The IDE fallback must not be short-circuited by the cached project."""
    calls = []
    _fresh_antigravity(monkeypatch, calls)

    antigravity.call_upstream("claude-haiku-4-5@default", MSGS, False)
    assert antigravity.ST["project"] == "proj-1"
    assert [p for u, p in calls if u.endswith("loadCodeAssist")] == [
        {"metadata": IDE_META}]

    # the fallback chain names an identity on purpose; reusing the cached
    # project would degenerate it into the request that just failed.
    antigravity.call_upstream("claude-haiku-4-5@default", MSGS, False,
                              ide="GEMINI_CLI")
    loads = [p for u, p in calls if u.endswith("loadCodeAssist")]
    assert loads[-1] == {"metadata": dict(IDE_META, ideType="GEMINI_CLI")}
    assert antigravity.ST["ide"] == "GEMINI_CLI"
    assert "metadata" not in calls[-1][1]


def test_call_upstream_keeps_the_ide_parameter():
    """test_chat_budget drives the IDE fallback through this kwarg."""
    params = inspect.signature(antigravity.call_upstream).parameters
    assert params["ide"].default is None


def test_the_gemini_envelope_hands_the_client_a_string_message(monkeypatch):
    def boom_a(model, msgs, stream, timeout=180, deadline=None):
        raise gemini.UpstreamError(
            "codeassist error: HTTP 403 Verify your account to continue.")

    def boom_b(prompt, timeout=180):
        raise RuntimeError("gemini-web: HTTP 403")

    monkeypatch.setattr(gemini, "call_a", boom_a)
    monkeypatch.setattr(gemini, "call_b", boom_b)
    # install.sh mints a key per bridge; an unset one leaves it open, and
    # the test post below carries no Authorization header.
    monkeypatch.setattr(gemini, "BRIDGE_KEY", "")
    bridge = _Bridge(gemini)
    try:
        code, body = bridge.post({"model": "gemini-2.5-flash", "messages": MSGS})
    finally:
        bridge.close()

    assert code == 502
    # an OpenAI-compatible client reads error.message as text: the
    # per-channel dict it used to carry made the whole envelope an object.
    assert isinstance(body["error"]["message"], str)
    assert "code_assist" in body["error"]["message"]
    assert "web" in body["error"]["message"]
    assert "HTTP 403" in body["error"]["message"]
    # the breakdown survives, one level down, for whoever reads the log
    assert body["error"]["type"] == "upstream_error"
    assert set(body["error"]["channels"]) == {"code_assist", "web"}
    assert body["error"]["channels"]["code_assist"].startswith(
        "codeassist error: HTTP 403")

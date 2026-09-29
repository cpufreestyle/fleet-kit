"""Every advertised codely alias must reach the gateway under its own name.

The bridge resolves the model name in three steps: undo the catalog's
<provider>/ namespacing, strip a provider prefix, then map a short alias onto
its full spelling. The whitelist is checked after all three. When one spelling
slips through the alias table -- the catalog advertises codely/codely-flash,
_strip_provider_prefix leaves "flash", and only the opencodex short alias "fl"
was mapped -- the bridge answers 400 "model 'flash' is not allowed" for a model
the Codex picker shows. A raw vendor name must still be refused locally instead
of burning a gateway round trip on a 401 team_model_access_denied.
"""
import importlib.util
import json
import os
import sys

import httpx
import pytest

BRIDGES = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, "bridges"))

LIVE_KEY = "sk-live000000000000live0000"
CHAT_REPLY = {"id": "chatcmpl-1", "object": "chat.completion", "model": "", "choices": [
    {"index": 0, "finish_reason": "stop",
     "message": {"role": "assistant", "content": "ok"}}]}

WRITINGS = {
    "codely/codely-core": "codely-core",
    "codely/codely-flash": "codely-flash",
    "codely/codely-air": "codely-air",
    "codely/codely-basic": "codely-basic",
    "codely/codely-vl": "codely-vl",
    "codely-core": "codely-core",
    "codely-flash": "codely-flash",
    "codely-air": "codely-air",
    "codely-basic": "codely-basic",
    "codely-vl": "codely-vl",
    "cdl/fl": "codely-flash",
    "fl": "codely-flash",
}


def _load_codely(tmp_path, monkeypatch):
    sys.path.insert(0, BRIDGES)
    spec = importlib.util.spec_from_file_location(
        "codely_alias_probe", os.path.join(BRIDGES, "codely", "codely_bridge.py"))
    codely = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(codely)
    creds = tmp_path / "oauth_creds.json"
    creds.write_text(json.dumps({"access_token": "ya29.fake", "cli_api_key": LIVE_KEY}))
    monkeypatch.setattr(codely, "creds_path", lambda: creds)
    monkeypatch.setattr(codely, "BRIDGE_KEY", "")
    import _common
    codely.check_bridge_auth = _common.make_auth_checker("")
    return codely


def _install_gateway(monkeypatch, codely, seen):
    def handler(request):
        assert request.url.host == "codely-litellm.tuanjie.cn", request.url.host
        body = json.loads(request.content)
        seen.append(body.get("model"))
        reply = dict(CHAT_REPLY, model=body.get("model"))
        return httpx.Response(200, json=reply)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(codely, "client", lambda: client)


@pytest.mark.parametrize("spelling,expected", sorted(WRITINGS.items()))
def test_every_alias_spelling_reaches_the_gateway(tmp_path, monkeypatch, spelling, expected):
    codely = _load_codely(tmp_path, monkeypatch)
    seen = []
    _install_gateway(monkeypatch, codely, seen)

    from fastapi.testclient import TestClient
    r = TestClient(codely.app).post("/v1/chat/completions", json={
        "model": spelling,
        "messages": [{"role": "user", "content": "hi"}],
    })

    assert r.status_code == 200, r.text
    assert seen == [expected], "upstream saw %r for %r" % (seen, spelling)
    assert r.json()["choices"][0]["message"]["content"] == "ok"


def test_a_raw_vendor_name_is_refused_locally(tmp_path, monkeypatch):
    """A name the team key can never use must not reach the gateway at all."""
    codely = _load_codely(tmp_path, monkeypatch)
    seen = []
    _install_gateway(monkeypatch, codely, seen)

    from fastapi.testclient import TestClient
    r = TestClient(codely.app).post("/v1/chat/completions", json={
        "model": "qwen3.8-max",
        "messages": [{"role": "user", "content": "hi"}],
    })

    assert r.status_code == 400
    assert "not allowed for this team key" in r.json()["detail"]
    assert seen == []


def test_a_name_saved_in_old_history_is_routed_to_the_compaction_model(
        tmp_path, monkeypatch):
    """Compaction must survive a session that still names a revoked model."""
    codely = _load_codely(tmp_path, monkeypatch)
    seen = []
    _install_gateway(monkeypatch, codely, seen)

    from fastapi.testclient import TestClient
    r = TestClient(codely.app).post("/v1/chat/completions", json={
        "model": "DeepSeek-V4.1-Flash",
        "messages": [{"role": "user", "content": "hi"}],
    })

    assert r.status_code == 200, r.text
    assert seen == [codely.COMPACTION_FALLBACK]


def test_the_alias_table_covers_every_whitelisted_model(tmp_path, monkeypatch):
    """<full alias> -> strip prefix -> table must still map back to itself."""
    codely = _load_codely(tmp_path, monkeypatch)
    for full in codely.FALLBACK_MODELS:
        stripped = codely._strip_provider_prefix(full)
        assert codely.ALIAS_TO_MODEL.get(stripped, stripped) == full, stripped

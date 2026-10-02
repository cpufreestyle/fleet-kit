"""The Kimi bridge must tell a lapsed plan apart from a dead key.

Measured 2026-10-03 against the real upstream: /v1/models answers 200 with the
key from the Kimi desktop app's own storage, while /v1/chat/completions answers
403 access_terminated_error because that account has no active plan. Reporting
that as a refused key sends the operator hunting for a credential that is
perfectly fine, so the route names the plan and the renewal page instead.

The other thing pinned here is where the upstream key comes from: the bridge
reads the Kimi desktop app's own key file as a fallback, because that key is
already on the machine. That fallback must stay opt-out-able and must never
become the local bridge key.
"""
import importlib.util
import json
import os
import sys

import httpx
import pytest

BRIDGES = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, "bridges"))
sys.path.insert(0, BRIDGES)

PATH = os.path.join(BRIDGES, "kimi", "kimi_bridge.py")
spec = importlib.util.spec_from_file_location("kimi_bridge", PATH)
kimi = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kimi)

UPSTREAM_CATALOG = {"object": "list", "data": [
    {"id": "kimi-for-coding", "object": "model"},
    {"id": "kimi-for-coding-highspeed", "object": "model"},
    {"id": "kimi-for-coding-tts", "object": "model"},
    {"id": "kimi-image-edit", "object": "model"},
]}
STATIC_CATALOG = ["kimi/kimi-for-coding", "kimi/kimi-for-coding-highspeed",
                  "kimi/k3", "kimi/k3-256k"]


def _reload_with_env(monkeypatch, **env):
    """A fresh copy of the bridge with the given env vars set."""
    for key in ("KIMI_CODING_API_KEY", "KIMI2CODEX_KEY", "KIMI_NO_APP_KEY",
                "KIMI_UPSTREAM_KEY_FILE"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    spec = importlib.util.spec_from_file_location("kimi_bridge_reloaded", PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def open_bridge():
    """The real app, with the local key check disabled for the tests."""
    import _common
    kimi.check_bridge_auth = _common.make_auth_checker("")
    return kimi


def _install_upstream(monkeypatch, *, status, body=None, boom=None, seen=None):
    def handler(request):
        if seen is not None:
            seen.append(request.headers.get("authorization"))
        if boom is not None:
            raise boom
        if status == 200:
            return httpx.Response(200, json=UPSTREAM_CATALOG)
        return httpx.Response(status, json=body or {"error": {"message": "nope"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(kimi, "client", lambda: client)


def _client(app):
    from fastapi.testclient import TestClient
    return TestClient(app)


def test_a_refused_key_is_reported_not_masked(open_bridge, monkeypatch):
    monkeypatch.setattr(kimi, "KEY", "sk-kimi-real")
    _install_upstream(monkeypatch, status=401)

    r = _client(kimi.app).get("/v1/models")

    assert r.status_code == 401
    assert r.json()["error"]["type"] == "upstream_auth_error"
    assert "KIMI_CODING_API_KEY" in r.json()["error"]["message"]
    assert "data" not in r.json()


def test_a_healthy_upstream_wins_and_junk_is_filtered(open_bridge, monkeypatch):
    monkeypatch.setattr(kimi, "KEY", "sk-kimi-real")
    _install_upstream(monkeypatch, status=200)

    r = _client(kimi.app).get("/v1/models")

    ids = [m["id"] for m in r.json()["data"]]
    # tts and image-edit rows cannot answer chat/completions; hiding them keeps
    # the picker honest
    assert ids == ["kimi/kimi-for-coding", "kimi/kimi-for-coding-highspeed"]
    assert "_fallback" not in r.json()


def test_no_key_still_serves_the_static_catalog(open_bridge, monkeypatch):
    monkeypatch.setattr(kimi, "KEY", "")

    r = _client(kimi.app).get("/v1/models")

    assert r.status_code == 200
    assert [m["id"] for m in r.json()["data"]] == STATIC_CATALOG
    assert "_fallback" not in r.json()


def test_a_lapsed_plan_is_not_reported_as_a_dead_key(open_bridge, monkeypatch):
    """The one case this bridge exists to phrase correctly."""
    monkeypatch.setattr(kimi, "KEY", "sk-kimi-real")
    _install_upstream(monkeypatch, status=403, body={
        "error": {"type": "access_terminated_error",
                  "message": "Your current subscription does not have access "
                             "to Kimi Code right now. Upgrade your plan."}})

    r = _client(kimi.app).post("/v1/chat/completions",
                               json={"model": "kimi/kimi-for-coding",
                                     "messages": [{"role": "user", "content": "hi"}]})

    assert r.status_code == 403
    err = r.json()["error"]
    assert err["type"] == "kimi_plan_inactive"
    assert "kimi.com/code/#pricing" in err["message"]
    # the key itself is fine, and the reply has to say so
    assert "the key itself is accepted" in err["message"]


def test_a_plain_403_keeps_the_generic_error(open_bridge, monkeypatch):
    monkeypatch.setattr(kimi, "KEY", "sk-kimi-real")
    _install_upstream(monkeypatch, status=403, body={
        "error": {"type": "rate_limit_error", "message": "slow down"}})

    r = _client(kimi.app).post("/v1/chat/completions",
                               json={"model": "kimi/kimi-for-coding",
                                     "messages": [{"role": "user", "content": "hi"}]})

    assert r.status_code == 403
    assert r.json()["error"]["type"] == "kimi_upstream_error"


def test_chat_without_any_key_names_both_ways_to_supply_one(open_bridge, monkeypatch):
    monkeypatch.setattr(kimi, "KEY", "")
    seen = []
    _install_upstream(monkeypatch, status=200, seen=seen)

    r = _client(kimi.app).post("/v1/chat/completions",
                               json={"model": "kimi/kimi-for-coding",
                                     "messages": [{"role": "user", "content": "hi"}]})

    assert r.status_code == 503
    message = r.json()["error"]["message"]
    assert "KIMI_CODING_API_KEY" in message
    assert "Kimi desktop app" in message
    assert seen == [], "no upstream call is worth making without a key"


def test_the_local_bridge_key_is_not_reused_upstream(monkeypatch):
    """A loopback token must never be shipped to a third party."""
    module = _reload_with_env(monkeypatch, KIMI2CODEX_KEY="sk-local-bridge-key")

    # The local key must never travel upstream. On a machine that also has a
    # Kimi desktop app the fallback legitimately supplies a different key,
    # so what is pinned is that the local one is not the one that is used --
    # and that with the fallback disabled nothing is used at all.
    assert module.KEY != "sk-local-bridge-key"
    r = _client(module.app).get("/health")
    assert r.json()["key_source"] != "KIMI2CODEX_KEY"

    plain = _reload_with_env(monkeypatch, KIMI2CODEX_KEY="sk-local-bridge-key",
                            KIMI_NO_APP_KEY="1")
    assert plain.KEY is None
    assert _client(plain.app).get("/health").json()["has_api_key"] is False


def test_the_desktop_app_key_file_is_read_as_a_fallback(monkeypatch, tmp_path):
    """The key already on this machine is the whole point of the fallback."""
    path = tmp_path / "daimon-share" / "daimon" / "kimi-code-key.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"v": 2, "keys": [
        {"userId": "u1", "apiKey": "sk-kimi-from-app", "keyId": "k"}]}),
        encoding="utf-8")
    module = _reload_with_env(monkeypatch, KIMI_UPSTREAM_KEY_FILE=str(path))

    assert module.KEY == "sk-kimi-from-app"
    assert module.KEY_WHERE == str(path)
    r = _client(module.app).get("/health")
    assert r.json()["has_api_key"] is True
    assert r.json()["key_source"] == str(path)


def test_the_app_key_fallback_is_opt_out(monkeypatch):
    module = _reload_with_env(monkeypatch, KIMI_NO_APP_KEY="1")

    assert module.KEY is None
    assert "KIMI_NO_APP_KEY" in module.KEY_WHERE


def test_a_broken_app_key_file_says_why(monkeypatch, tmp_path):
    path = tmp_path / "kimi-code-key.json"
    path.write_text("{not json", encoding="utf-8")
    module = _reload_with_env(monkeypatch, KIMI_UPSTREAM_KEY_FILE=str(path))

    assert module.KEY is None
    assert "JSONDecodeError" in module.KEY_WHERE


def test_an_env_key_wins_over_the_app_file(monkeypatch, tmp_path):
    path = tmp_path / "kimi-code-key.json"
    path.write_text(json.dumps({"v": 2, "keys": [{"apiKey": "sk-kimi-from-app"}]}),
                    encoding="utf-8")
    module = _reload_with_env(monkeypatch, KIMI_UPSTREAM_KEY_FILE=str(path),
                              KIMI_CODING_API_KEY="sk-kimi-from-env")

    assert module.KEY == "sk-kimi-from-env"
    assert module.KEY_WHERE == "KIMI_CODING_API_KEY"


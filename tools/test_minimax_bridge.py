"""The MiniMax bridge must name the credential it wants, and its region.

Measured 2026-10-03: api.minimaxi.com/v1 answers 401 authorized_error(1004)
without a key, and a key minted for the international console is refused by the
CN host in exactly the same shape. Telling an operator "your key is dead" sends
them to regenerate a key that is fine, so the refusal message names both fixes.

The other thing pinned here is the static catalog: with no key the upstream
/v1/models cannot be opened, so the documented model list is the only thing the
picker can show. It is taken from MiniMax's own model overview, quality first,
and it must survive a reload because an unset key is the normal state on a fresh
install.
"""
import importlib.util
import os
import sys

import httpx
import pytest

BRIDGES = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, "bridges"))
sys.path.insert(0, BRIDGES)

PATH = os.path.join(BRIDGES, "minimax", "minimax_bridge.py")
spec = importlib.util.spec_from_file_location("minimax_bridge", PATH)
minimax = importlib.util.module_from_spec(spec)
spec.loader.exec_module(minimax)

UPSTREAM_CATALOG = {"object": "list", "data": [
    {"id": "MiniMax-M3", "object": "model"},
    {"id": "MiniMax-M2.7", "object": "model"},
    {"id": "speech-2.8-turbo", "object": "model"},
    {"id": "MiniMax-Hailuo-2.3", "object": "model"},
]}
STATIC_CATALOG = [
    "minimax/MiniMax-M3.1-Flash-Preview",
    "minimax/MiniMax-M3",
    "minimax/MiniMax-M2.7",
    "minimax/MiniMax-M2.7-highspeed",
    "minimax/MiniMax-M2.5",
    "minimax/MiniMax-M2.5-highspeed",
    "minimax/MiniMax-M2.1",
    "minimax/MiniMax-M2.1-highspeed",
    "minimax/MiniMax-M2",
]


def _reload_with_env(monkeypatch, **env):
    """A fresh copy of the bridge with the given env vars set."""
    for key in ("MINIMAX_API_KEY", "MINIMAX2CODEX_KEY"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    spec = importlib.util.spec_from_file_location("minimax_bridge_reloaded", PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def open_bridge():
    """The real app, with the local key check disabled for the tests."""
    import _common
    minimax.check_bridge_auth = _common.make_auth_checker("")
    return minimax


def _install_upstream(monkeypatch, *, status, seen=None, boom=None):
    def handler(request):
        if seen is not None:
            seen.append(request.headers.get("authorization"))
        if boom is not None:
            raise boom
        if status == 200:
            return httpx.Response(200, json=UPSTREAM_CATALOG)
        return httpx.Response(status, json={
            "error": {"type": "authorized_error",
                      "message": "login fail: Please carry the API secret key"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(minimax, "client", lambda: client)


def _client(app):
    from fastapi.testclient import TestClient
    return TestClient(app)


def test_a_refused_key_names_the_region_fix_too(open_bridge, monkeypatch):
    monkeypatch.setattr(minimax, "API_KEY", "mm-key")
    _install_upstream(monkeypatch, status=401)

    r = _client(minimax.app).get("/v1/models")

    assert r.status_code == 401
    err = r.json()["error"]
    assert err["type"] == "upstream_auth_error"
    assert "MINIMAX_API_KEY" in err["message"]
    assert "api.minimax.io" in err["message"], "a region mismatch needs its own hint"
    assert "data" not in r.json()


def test_a_healthy_upstream_wins_and_junk_is_filtered(open_bridge, monkeypatch):
    monkeypatch.setattr(minimax, "API_KEY", "mm-key")
    _install_upstream(monkeypatch, status=200)

    r = _client(minimax.app).get("/v1/models")

    ids = [m["id"] for m in r.json()["data"]]
    # speech and video rows cannot answer chat/completions
    assert ids == ["minimax/MiniMax-M3", "minimax/MiniMax-M2.7"]
    assert "_fallback" not in r.json()


def test_an_unreachable_upstream_still_serves_the_catalog(open_bridge, monkeypatch):
    monkeypatch.setattr(minimax, "API_KEY", "mm-key")
    _install_upstream(monkeypatch, status=0, boom=httpx.ConnectError("no route"))

    r = _client(minimax.app).get("/v1/models")

    assert r.status_code == 200
    assert [m["id"] for m in r.json()["data"]] == STATIC_CATALOG
    assert "ConnectError" in r.json()["_fallback"]


def test_no_key_at_all_serves_the_documented_catalog(open_bridge, monkeypatch):
    """A fresh install has no key, so the static rows are the picker."""
    monkeypatch.setattr(minimax, "API_KEY", "")

    r = _client(minimax.app).get("/v1/models")

    assert r.status_code == 200
    assert [m["id"] for m in r.json()["data"]] == STATIC_CATALOG
    assert "_fallback" not in r.json()
    assert r.json()["data"][0]["owned_by"] == "minimax"


def test_the_local_bridge_key_is_not_reused_upstream(monkeypatch):
    """A loopback token must never be shipped to a third party."""
    module = _reload_with_env(monkeypatch, MINIMAX2CODEX_KEY="sk-local-bridge-key")

    assert module.API_KEY == ""
    r = _client(module.app).get("/health")
    assert r.json()["has_api_key"] is False


def test_chat_without_any_key_fails_locally_with_the_fix(open_bridge, monkeypatch):
    monkeypatch.setattr(minimax, "API_KEY", "")
    seen = []
    _install_upstream(monkeypatch, status=200, seen=seen)

    r = _client(minimax.app).post("/v1/chat/completions",
                                  json={"model": "minimax/MiniMax-M2.7",
                                        "messages": [{"role": "user", "content": "hi"}]})

    assert r.status_code == 503
    err = r.json()["error"]
    assert err["type"] == "minimax_key_missing"
    assert "MINIMAX_API_KEY" in err["message"]
    assert "finish.sh minimax" in err["message"]
    assert seen == [], "no upstream call is worth making without a key"


def test_a_local_key_on_the_wire_is_not_required(open_bridge, monkeypatch):
    """An unset local key leaves the bridge open, as _common documents."""
    monkeypatch.setattr(minimax, "API_KEY", "mm-key")
    _install_upstream(monkeypatch, status=200)

    r = _client(minimax.app).get("/v1/models")

    assert r.status_code == 200


def test_the_model_name_is_stripped_of_the_catalog_prefix(open_bridge, monkeypatch):
    """Codex sends minimax/<model>; the upstream wants the bare id."""
    monkeypatch.setattr(minimax, "API_KEY", "mm-key")
    seen = []
    _install_upstream(monkeypatch, status=200, seen=seen)

    from fastapi.testclient import TestClient
    with TestClient(minimax.app) as c:
        c.post("/v1/chat/completions",
               json={"model": "minimax/MiniMax-M3", "messages": []})

    assert seen == ["Bearer mm-key"]


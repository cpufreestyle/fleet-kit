"""A refused upstream key must not be advertised as a usable model list.

Measured 2026-09-29: fleet.env carries QWEN2CODEX_KEY (the local bridge key) but
no QWEN_API_KEY. The bridge reused the local key upstream, so Qwen Cloud
answered 401 for it while /v1/models still returned the static two-row catalog
marked only by a "_fallback" note nobody reads -- /health and the picker stayed
green while every single chat call 401'd. The route now reports the refusal as a
401, and the bridge key is no longer reused upstream at all.

The route now reports the refusal as a 401 instead. These tests pin that, and
pin the two cases that must still be answered with the real catalog: the key is
real but the upstream is unreachable, and there is no key configured at all.
"""
import importlib.util
import os
import sys

import httpx
import pytest

BRIDGES = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, "bridges"))
sys.path.insert(0, BRIDGES)

spec = importlib.util.spec_from_file_location(
    "qwen_bridge", os.path.join(BRIDGES, "qwen", "qwen_bridge.py"))
qwen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qwen)

UPSTREAM_CATALOG = {"object": "list", "data": [
    {"id": "qwen3.8-flash", "object": "model"},
    {"id": "qwen3-max", "object": "model"},
    {"id": "qwen-tts", "object": "model"},
    {"id": "qwen3-embedding", "object": "model"},
]}
STATIC_CATALOG = ["qwen/qwen3.8-flash", "qwen/qwen3.8-max"]


def _reload_with_env(monkeypatch, **env):
    """Import a fresh copy of the bridge with the given env vars set."""
    import importlib
    for key in ("QWEN_API_KEY", "QWEN2CODEX_KEY"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    spec = importlib.util.spec_from_file_location(
        "qwen_bridge_reloaded", os.path.join(BRIDGES, "qwen", "qwen_bridge.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def open_bridge():
    """The real app, with the local key check disabled for the tests."""
    import _common
    qwen.check_bridge_auth = _common.make_auth_checker("")
    return qwen


def _install_upstream(monkeypatch, *, status, seen=None, boom=None):
    def handler(request):
        if seen is not None:
            seen.append(request.headers.get("authorization"))
        if boom is not None:
            raise boom
        if status == 200:
            return httpx.Response(200, json=UPSTREAM_CATALOG)
        return httpx.Response(status, json={"error": {"message": "Invalid API-key"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(qwen, "client", lambda: client)


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_key_is_reported_not_masked(open_bridge, monkeypatch, status):
    monkeypatch.setattr(qwen, "API_KEY", "sk-local-qwen")
    _install_upstream(monkeypatch, status=status)

    from fastapi.testclient import TestClient
    r = TestClient(qwen.app).get("/v1/models")

    assert r.status_code == 401
    body = r.json()
    assert body["error"]["type"] == "upstream_auth_error"
    assert "QWEN_API_KEY" in body["error"]["message"]
    # the unusable catalog is nowhere in the reply
    assert "data" not in body


def test_a_network_failure_still_serves_the_real_catalog(open_bridge, monkeypatch):
    """A 500 is upstream's problem; the model names themselves are real."""
    monkeypatch.setattr(qwen, "API_KEY", "sk-real-key")
    _install_upstream(monkeypatch, status=500)

    from fastapi.testclient import TestClient
    r = TestClient(qwen.app).get("/v1/models")

    assert r.status_code == 200
    assert [m["id"] for m in r.json()["data"]] == STATIC_CATALOG
    assert r.json()["_fallback"] == "upstream 500"


def test_an_unreachable_upstream_still_serves_the_catalog(open_bridge, monkeypatch):
    monkeypatch.setattr(qwen, "API_KEY", "sk-real-key")
    _install_upstream(monkeypatch, status=0, boom=httpx.ConnectError("network unreachable"))

    from fastapi.testclient import TestClient
    r = TestClient(qwen.app).get("/v1/models")

    assert r.status_code == 200
    assert [m["id"] for m in r.json()["data"]] == STATIC_CATALOG
    assert "ConnectError" in r.json()["_fallback"]


def test_no_key_at_all_still_serves_the_catalog_without_a_fallback_note(
        open_bridge, monkeypatch):
    """With no key there was no upstream call to fail, so there is no detail."""
    monkeypatch.setattr(qwen, "API_KEY", "")

    from fastapi.testclient import TestClient
    r = TestClient(qwen.app).get("/v1/models")

    assert r.status_code == 200
    assert [m["id"] for m in r.json()["data"]] == STATIC_CATALOG
    assert "_fallback" not in r.json()


def test_the_local_bridge_key_is_not_reused_upstream(monkeypatch):
    """Regression: a set QWEN2CODEX_KEY must not become the upstream key.

    Measured 2026-09-29: with QWEN_API_KEY unset the bridge sent its own local
    key to maas.qwencloudapi.com, /health answered has_api_key=true, and every
    upstream reply was a 401 that read like an expired session.
    """
    module = _reload_with_env(monkeypatch, QWEN2CODEX_KEY="sk-local-bridge-key")

    assert module.API_KEY == ""
    from fastapi.testclient import TestClient
    r = TestClient(module.app).get("/health")
    assert r.json()["has_api_key"] is False


def test_chat_without_any_key_fails_locally_with_the_fix_hint(open_bridge, monkeypatch):
    """No key at all: fail locally with the setup fix, call no upstream."""
    monkeypatch.setattr(qwen, "API_KEY", "")
    seen = []
    _install_upstream(monkeypatch, status=200, seen=seen)

    from fastapi.testclient import TestClient
    r = TestClient(qwen.app).post("/v1/chat/completions",
                                  json={"model": "qwen/qwen3.8-flash", "messages": []})

    assert r.status_code == 503
    assert r.json()["error"]["type"] == "qwen_key_missing"
    assert "QWEN_API_KEY" in r.json()["error"]["message"]
    assert seen == []


def test_a_healthy_upstream_is_filtered_but_not_prefixed_twice(open_bridge, monkeypatch):
    """Regression guard: the real catalog still wins when the key works."""
    monkeypatch.setattr(qwen, "API_KEY", "sk-real-key")
    _install_upstream(monkeypatch, status=200)

    from fastapi.testclient import TestClient
    r = TestClient(qwen.app).get("/v1/models")

    ids = [m["id"] for m in r.json()["data"]]
    # tts / embedding are marketplace models the chat bridge must not expose
    assert ids == ["qwen/qwen3.8-flash", "qwen/qwen3-max"]
    assert "_fallback" not in r.json()
    assert r.json()["data"][0]["owned_by"] == "qwen-cloud"

"""A revoked LiteLLM virtual key must not disable the bridge.

Measured 2026-09-29: after a subscription/org change the gateway revokes the
virtual key that ~/.codely-cli/oauth_creds.json still holds. GET /v1/models
then answers 401 "Unable to find token in cache or LiteLLM_VerificationTokenTable",
while the cli-api-key endpoint hands out a different, working key for the same
access token. The bridge kept the dead key forever because

  * /v1/models had no 401 recovery at all, so it served the 5-row fallback
    catalog even though a good key was one call away;
  * the chat path did recover, but it called refresh_access_token() first,
    which needs a refresh_token the official CLI never writes; its
    HTTPException was swallowed by "except HTTPException: pass" and the retry
    went out with the same dead key.

These tests pin the fixed behaviour for both routes.
"""
import importlib.util
import json
import os
import sys

import httpx
import pytest

BRIDGES = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, "bridges"))
sys.path.insert(0, BRIDGES)

DEAD_KEY = "sk-deaddeaddeaddeaddeaddead"
GOOD_KEY = "sk-live000000000000live0000"
GATEWAY_CATALOG = {"object": "list", "data": [
    {"id": "codely-core", "object": "model", "created": 0, "owned_by": "tuanjie-ai"},
    {"id": "codely-flash", "object": "model", "created": 0, "owned_by": "tuanjie-ai"},
]}


def _load_codely(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "codely_bridge", os.path.join(BRIDGES, "codely", "codely_bridge.py"))
    codely = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(codely)
    # A private CLI home so the test never touches the real login state.
    creds = tmp_path / "oauth_creds.json"
    creds.write_text(json.dumps({
        "access_token": "ya29.fake-access-token",
        "cli_api_key": DEAD_KEY,
        "user_id": 45391,
        "rpm": 0,
        "tpm": 0,
    }))
    monkeypatch.setattr(codely, "creds_path", lambda: creds)
    monkeypatch.setattr(codely, "BRIDGE_KEY", "")
    # The checker is bound at import time from BRIDGE_KEY, which the test
    # environment already supplies, so rebind it to the open policy.
    import _common
    codely.check_bridge_auth = _common.make_auth_checker("")
    return codely, creds


def _install_gateway(monkeypatch, codely, *, minted=GOOD_KEY, mint_status=200,
                     gateway_status=None, seen=None):
    """Route SERVER (token mint) and GATEWAY (LiteLLM) through one mock."""
    def handler(request):
        host = request.url.host
        if host == "codely.tuanjie.cn":
            assert request.url.path == "/api/api-token/cli-api-key"
            assert request.headers.get("authorization") == "Bearer ya29.fake-access-token"
            if mint_status != 200:
                return httpx.Response(mint_status, json={"error": "nope"})
            return httpx.Response(200, json={"cli_api_key": minted, "user_id": 45391})
        if host == "codely-litellm.tuanjie.cn":
            if seen is not None:
                seen.append(request.headers.get("authorization"))
            status = gateway_status
            if status is None:
                status = 401 if request.headers.get("authorization") == "Bearer " + DEAD_KEY else 200
            body = GATEWAY_CATALOG if status == 200 else {"error": {
                "message": "Authentication Error, Invalid proxy server token passed"}}
            return httpx.Response(status, json=body)
        raise AssertionError("unexpected host " + host)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(codely, "client", lambda: client)
    return client


def test_models_route_remints_a_dead_key(tmp_path, monkeypatch):
    codely, creds = _load_codely(tmp_path, monkeypatch)
    seen = []
    _install_gateway(monkeypatch, codely, seen=seen)

    from fastapi.testclient import TestClient
    r = TestClient(codely.app).get("/v1/models")

    assert r.status_code == 200
    assert len(r.json()["data"]) == len(GATEWAY_CATALOG["data"])
    # the dead key was tried once, then the re-minted key
    assert seen == ["Bearer " + DEAD_KEY, "Bearer " + GOOD_KEY]
    # ... and the good key was persisted, so the next boot needs no recovery
    assert json.loads(creds.read_text())["cli_api_key"] == GOOD_KEY
    assert "X-Codely-Models-Fallback" not in r.headers


def test_models_route_still_degrades_when_nothing_can_be_minted(tmp_path, monkeypatch):
    codely, _ = _load_codely(tmp_path, monkeypatch)
    _install_gateway(monkeypatch, codely, mint_status=403)

    from fastapi.testclient import TestClient
    r = TestClient(codely.app).get("/v1/models")

    assert r.status_code == 200
    assert r.headers.get("X-Codely-Models-Fallback")
    assert len(r.json()["data"]) == len(codely.FALLBACK_MODELS)


def test_models_route_terminal_401_degrades_with_a_sanitised_header(
        tmp_path, monkeypatch):
    """Even when the key cannot be recovered, the reply must stay parseable.

    The upstream body carries newlines; forwarding it as a header made h11
    abort the response, so the client saw an empty reply instead of the
    fallback catalog it was meant to get.
    """
    codely, _ = _load_codely(tmp_path, monkeypatch)
    # Gateway rejects every key, including a freshly minted one.
    _install_gateway(monkeypatch, codely, gateway_status=401)

    from fastapi.testclient import TestClient
    r = TestClient(codely.app).get("/v1/models")

    assert r.status_code == 200
    assert "X-Codely-Models-Fallback" in r.headers
    assert "\n" not in r.headers["X-Codely-Models-Fallback"]
    assert len(r.json()["data"]) == len(codely.FALLBACK_MODELS)


def test_remint_gateway_key_prefers_remint_over_access_refresh(tmp_path, monkeypatch):
    """The old order refreshed the access token first; that cannot work here."""
    codely, creds = _load_codely(tmp_path, monkeypatch)
    _install_gateway(monkeypatch, codely)

    import asyncio
    key = asyncio.run(codely.remint_gateway_key())
    assert key == GOOD_KEY
    # refresh_access_token() must not have been consulted (no refresh_token here)
    assert "refresh_token" not in json.loads(creds.read_text())


def test_chat_route_retries_with_the_reminted_key(tmp_path, monkeypatch):
    codely, creds = _load_codely(tmp_path, monkeypatch)
    seen = []
    _install_gateway(monkeypatch, codely, seen=seen)

    from fastapi.testclient import TestClient
    r = TestClient(codely.app).post("/v1/chat/completions", json={
        "model": "codely/codely-core",
        "messages": [{"role": "user", "content": "hi"}],
    })

    assert r.status_code == 200
    assert len(seen) == 2
    assert seen[0] == "Bearer " + DEAD_KEY
    assert seen[1] == "Bearer " + GOOD_KEY
    assert json.loads(creds.read_text())["cli_api_key"] == GOOD_KEY

def test_models_route_degrades_when_the_gateway_is_unreachable(tmp_path, monkeypatch):
    """httpx.ConnectError must not become a bare 500.

    The gateway lives on the company network, so an unreachable host is the
    routine failure -- and it was the one class the try block did not catch.
    Letting it escape turned every /v1/models call into a bare 500 plus a
    traceback in the log (measured 2026-09-30: 291KB of ConnectError
    tracebacks) and bypassed the fallback catalog this handler exists to
    serve, so the panel reported the bridge down with an empty model list.
    """
    codely, _ = _load_codely(tmp_path, monkeypatch)

    def boom(request):
        raise httpx.ConnectError("All connection attempts failed")

    client = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    monkeypatch.setattr(codely, "client", lambda: client)

    from fastapi.testclient import TestClient
    r = TestClient(codely.app).get("/v1/models")

    assert r.status_code == 200
    assert [m["id"] for m in r.json()["data"]] == [
        codely.CATALOG_PREFIX + m for m in codely.FALLBACK_MODELS]
    assert r.headers.get("X-Codely-Models-Fallback")
    assert "\n" not in r.headers["X-Codely-Models-Fallback"]



def test_chat_route_degrades_when_the_gateway_is_unreachable(tmp_path, monkeypatch):
    """The chat route must not answer a bare 500 when the gateway drops.

    Same hole as /v1/models had before root cause 24, measured 2026-09-30 by
    injecting ConnectError into the only upstream call the chat route makes:
    the exception escaped the handler entirely and the caller got
    500 Internal Server Error plus a traceback, with nothing in the body
    about the cause. The upstream error envelope already exists for exactly
    this wording ("an unreachable-host error, which is not an upstream status
    at all"), so the fix is to route through it.
    """
    codely, _ = _load_codely(tmp_path, monkeypatch)

    def boom(request):
        raise httpx.ConnectError("All connection attempts failed")

    client = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    monkeypatch.setattr(codely, "client", lambda: client)

    from fastapi.testclient import TestClient
    r = TestClient(codely.app).post("/v1/chat/completions", json={
        "model": "codely/codely-core",
        "messages": [{"role": "user", "content": "hi"}],
    })

    assert r.status_code == 503
    err = r.json()["error"]
    assert "unreachable" in err["message"]
    assert "ConnectError" in err["message"]
    assert err["type"] == "codely_upstream_unreachable"


def test_chat_route_keeps_the_whitelist_400_when_the_gateway_drops(
        tmp_path, monkeypatch):
    """The new catch must not swallow the bridge's own 4xx decisions.

    HTTPException is raised before any upstream call, so an unknown model
    still has to fail with the whitelist 400 -- not 503 -- while the network
    is equally unusable.
    """
    codely, _ = _load_codely(tmp_path, monkeypatch)

    def boom(request):
        raise httpx.ConnectError("All connection attempts failed")

    client = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    monkeypatch.setattr(codely, "client", lambda: client)

    from fastapi.testclient import TestClient
    r = TestClient(codely.app).post("/v1/chat/completions", json={
        "model": "codely/not-on-this-team-key",
        "messages": [{"role": "user", "content": "hi"}],
    })

    assert r.status_code == 400
    assert "is not allowed for this team key" in r.json()["detail"]

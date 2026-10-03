"""The MiniMax bridge must name the credential it wants, and its region.

Measured 2026-10-03: api.minimaxi.com/v1 answers 401 authorized_error(1004)
without a key, and a key minted for the international console is refused by
the CN host in exactly the same shape. Telling an operator "your key is dead"
sends them to regenerate a key that is fine, so the refusal message names
both fixes: where MINIMAX_API_KEY goes, and the international endpoint a
foreign key actually wants.

The second thing pinned here is the static catalog: with no key the upstream
/models cannot be opened, so the documented model list is the only thing the
picker can show. It is taken from MiniMax's own model overview, quality
first. MiniMax's /v1 also carries speech, video, image and music rows; a
chat bridge advertising them hands the picker rows that can only 4xx, so the
junk filter is pinned on the same measured catalog.

The third is the account pool (bridges/plan_key_pool.py): the bridge holds
one key per account, so a 401 on one key has to move to the next account
instead of taking the node down, and a second key has to be addable while
the service is running. The generic pool has its own tests; what is pinned
here is the MiniMax wiring -- seeding, the 401/403 vocabulary, and the admin
endpoints an operator actually calls.
"""
import importlib.util
import json
import os
import sys
from pathlib import Path

import httpx
import pytest

BRIDGES = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, "bridges"))
sys.path.insert(0, BRIDGES)

PATH = os.path.join(BRIDGES, "minimax", "minimax_bridge.py")
_spec = importlib.util.spec_from_file_location("minimax_bridge_ut", PATH)

# the upstream catalog as measured: chat rows plus the junk that must not leak
UPSTREAM_CATALOG = {"object": "list", "data": [
    {"id": "MiniMax-M3", "object": "model"},
    {"id": "MiniMax-M2.7", "object": "model"},
    {"id": "speech-2.8-turbo", "object": "model"},
    {"id": "MiniMax-Hailuo-2.3", "object": "model"},
]}

# the bridge's own documented rows, quality first, with the catalog prefix
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


def _load(monkeypatch, tmp_path, **env):
    """A private copy of the bridge, its pool rooted in tmp_path.

    The env patch is scoped to the import on purpose: the pool directory and
    the keys are read once, while the module is being built. A pool dir
    outside tmp_path would put keys on the developer's own machine. The
    points TTL is pushed past the epoch so /health never schedules the real
    (blocking, network) reader in a unit test; the forced refresh behind
    /admin/pool/points ignores the TTL and is what the points test calls.
    """
    settings = {"MINIMAX_AUTH_POOL_DIR": str(tmp_path / "auths"),
                "MINIMAX_POINTS_TTL": "1000000000000"}
    for key in ("MINIMAX_API_KEY", "MINIMAX_API_KEYS", "MINIMAX2CODEX_KEY",
                "MINIMAX_UPSTREAM", "MINIMAX_UPSTREAM_PROXY"):
        monkeypatch.delenv(key, raising=False)
    settings.update(env)
    for key, value in settings.items():
        monkeypatch.setenv(key, value)
    module = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(module)
    return module


def _install_upstream(module, monkeypatch, *, status=200, body=None, boom=None,
                      keys=None, seen=None, bodies=None):
    """MockTransport answers /models and /chat, recording what was offered."""
    def handler(request):
        auth = (request.headers.get("authorization") or "")
        if seen is not None:
            seen.append(auth)
        if bodies is not None:
            try:
                bodies.append(json.loads(request.content or b"{}"))
            except ValueError:
                bodies.append(None)
        if boom is not None:
            raise boom
        # one answer per key: keys maps an authorization header to what it
        # answers, anything not in it (and the whole map when there is none)
        # gets the canned status
        answer = keys.get(auth, 200) if keys is not None else status
        if answer == 200:
            return httpx.Response(200, json=UPSTREAM_CATALOG)
        return httpx.Response(answer, json=body or {
            "error": {"type": "authorized_error",
                      "message": "login fail: Please carry the API secret key"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(module, "client", lambda: client)


def _client(module):
    from fastapi.testclient import TestClient
    return TestClient(module.app)


# ---------------- what the caller sees ----------------

def test_a_refused_key_names_the_region_fix_too(monkeypatch, tmp_path):
    module = _load(monkeypatch, tmp_path, MINIMAX_API_KEY="sk-mm-real")
    _install_upstream(module, monkeypatch, status=401)

    r = _client(module).get("/v1/models")

    assert r.status_code == 401
    err = r.json()["error"]
    assert err["type"] == "upstream_auth_error"
    assert "MINIMAX_API_KEY" in err["message"]
    assert "api.minimax.io" in err["message"], "a region mismatch needs its own hint"
    assert "data" not in r.json()


def test_a_healthy_upstream_wins_and_junk_is_filtered(monkeypatch, tmp_path):
    module = _load(monkeypatch, tmp_path, MINIMAX_API_KEY="sk-mm-real")
    _install_upstream(module, monkeypatch, status=200)

    r = _client(module).get("/v1/models")

    ids = [m["id"] for m in r.json()["data"]]
    # speech and video rows cannot answer chat/completions
    assert ids == ["minimax/MiniMax-M3", "minimax/MiniMax-M2.7"]
    assert "_fallback" not in r.json()


def test_no_key_still_serves_the_documented_catalog(monkeypatch, tmp_path):
    """A fresh install has no key, so the static rows are the picker."""
    module = _load(monkeypatch, tmp_path)

    r = _client(module).get("/v1/models")

    assert r.status_code == 200
    assert [m["id"] for m in r.json()["data"]] == STATIC_CATALOG
    assert "_fallback" not in r.json()
    assert r.json()["data"][0]["owned_by"] == "minimax"


def test_an_unreachable_upstream_keeps_the_catalog_and_names_the_layer(
        monkeypatch, tmp_path):
    module = _load(monkeypatch, tmp_path, MINIMAX_API_KEY="sk-mm-real")
    _install_upstream(module, monkeypatch, boom=httpx.ConnectError("no route"))

    r = _client(module).get("/v1/models")

    # a dead network is not the account's fault: the documented catalog still
    # beats an error page, and the row says which layer broke
    assert r.status_code == 200
    assert [m["id"] for m in r.json()["data"]] == STATIC_CATALOG
    assert "ConnectError" in r.json()["_fallback"]


def test_a_cooling_pool_still_serves_the_catalog(monkeypatch, tmp_path):
    """Every key cooling must not take the documented list down with it."""
    module = _load(monkeypatch, tmp_path, MINIMAX_API_KEY="sk-mm-cool")
    _install_upstream(module, monkeypatch, keys={"Bearer sk-mm-cool": 401})

    with _client(module) as client:
        first = client.get("/v1/models")
        assert first.status_code == 401
        second = client.get("/v1/models")
        assert second.status_code == 200
        assert [m["id"] for m in second.json()["data"]] == STATIC_CATALOG
        assert second.json()["_fallback"] == "account pool cooling"


def test_the_local_bridge_key_is_not_reused_upstream(monkeypatch, tmp_path):
    """A loopback token must never be shipped to a third party."""
    module = _load(monkeypatch, tmp_path, MINIMAX2CODEX_KEY="sk-local-bridge-key")
    seen = []
    _install_upstream(module, monkeypatch, status=200, seen=seen)

    assert module.POOL.status() == []
    r = _client(module).get("/health")
    assert r.json()["has_api_key"] is False
    assert r.json()["key_source"] == "\u65e0 key"
    assert seen == []


def test_an_international_key_can_point_at_the_international_host(
        monkeypatch, tmp_path):
    """MINIMAX_UPSTREAM is the whole fix for a foreign-account key."""
    module = _load(monkeypatch, tmp_path, MINIMAX_API_KEY="sk-mm-real",
                   MINIMAX_UPSTREAM="https://api.minimax.io/v1")

    r = _client(module).get("/health")

    assert r.json()["upstream"] == "https://api.minimax.io/v1"


def test_chat_without_any_key_fails_locally_with_the_fix(monkeypatch, tmp_path):
    module = _load(monkeypatch, tmp_path)
    seen = []
    _install_upstream(module, monkeypatch, status=200, seen=seen)

    r = _client(module).post("/v1/chat/completions",
                             json={"model": "minimax/MiniMax-M2.7",
                                   "messages": [{"role": "user", "content": "hi"}]})

    assert r.status_code == 503
    err = r.json()["error"]
    assert err["type"] == "minimax_key_missing"
    assert "MINIMAX_API_KEY" in err["message"]
    assert "finish.sh minimax" in err["message"]
    assert seen == [], "no upstream call is worth making without a key"


def test_a_dead_key_through_chat_names_the_region_fix(monkeypatch, tmp_path):
    module = _load(monkeypatch, tmp_path, MINIMAX_API_KEY="sk-mm-real")
    _install_upstream(module, monkeypatch, status=401)

    r = _client(module).post("/v1/chat/completions",
                             json={"model": "minimax/MiniMax-M3",
                                   "messages": [{"role": "user", "content": "hi"}]})

    assert r.status_code == 401
    err = r.json()["error"]
    assert err["type"] == "upstream_auth_error"
    assert "MINIMAX_API_KEY" in err["message"]
    assert "api.minimax.io" in err["message"]


def test_a_forbidden_request_keeps_the_generic_error(monkeypatch, tmp_path):
    """A 403 is the request, not the key, so it must not read as auth."""
    module = _load(monkeypatch, tmp_path, MINIMAX_API_KEY="sk-mm-real")
    _install_upstream(module, monkeypatch, status=403)

    r = _client(module).post("/v1/chat/completions",
                             json={"model": "minimax/MiniMax-M3",
                                   "messages": [{"role": "user", "content": "hi"}]})

    assert r.status_code == 403
    assert r.json()["error"]["type"] == "minimax_upstream_error"


def test_the_model_name_is_stripped_of_the_catalog_prefix(monkeypatch, tmp_path):
    """Codex sends minimax/<model>; the upstream wants the bare id."""
    module = _load(monkeypatch, tmp_path, MINIMAX_API_KEY="sk-mm-real")
    seen, bodies = [], []
    _install_upstream(module, monkeypatch, status=200, seen=seen, bodies=bodies)

    r = _client(module).post("/v1/chat/completions",
                             json={"model": "minimax/MiniMax-M3", "messages": []})

    assert r.status_code == 200
    assert seen == ["Bearer sk-mm-real"]
    assert bodies[0]["model"] == "MiniMax-M3"


# ---------------- the pool ----------------

def test_a_refused_key_fails_over_to_the_next_account(monkeypatch, tmp_path):
    module = _load(monkeypatch, tmp_path,
                   MINIMAX_API_KEYS="sk-mm-first,sk-mm-second")
    seen = []
    _install_upstream(module, monkeypatch,
                      keys={"Bearer sk-mm-first": 401}, seen=seen)

    r = _client(module).get("/v1/models")

    assert r.status_code == 200
    assert [m["id"] for m in r.json()["data"]] == \
        ["minimax/MiniMax-M3", "minimax/MiniMax-M2.7"]
    assert seen == ["Bearer sk-mm-first", "Bearer sk-mm-second"]
    # the key that failed is cooling, and /health says so
    health = _client(module).get("/health").json()
    states = {item["key_tail"]: item["state"]
              for item in health["account_pool"]["accounts"]}
    assert states == {"irst": "cooling", "cond": "ready"}
    assert health["account_pool"]["count"] == 2


def test_a_second_key_can_be_added_while_the_service_is_running(monkeypatch, tmp_path):
    module = _load(monkeypatch, tmp_path, MINIMAX_API_KEY="sk-mm-first")
    _install_upstream(module, monkeypatch, keys={"Bearer sk-mm-first": 401})

    with _client(module) as client:
        added = client.post("/admin/pool/add", json={"key": "sk-mm-second"})
        assert added.status_code == 200
        assert added.json()["skipped"] == []
        pool = added.json()["account_pool"]
        assert pool["count"] == 2

        r = client.get("/v1/models")
        assert r.status_code == 200
        assert "_fallback" not in r.json()

        # the source an account came from is what the panel shows
        sources = {item["key_tail"]: item["source"]
                   for item in client.get("/health").json()["account_pool"]["accounts"]}
        assert sources == {"irst": "env MINIMAX_API_KEY", "cond": "admin"}


def test_points_reach_health_for_the_panel(monkeypatch, tmp_path):
    module = _load(monkeypatch, tmp_path, MINIMAX_API_KEY="sk-mm-real")
    read = []
    module.POOL._read_points = lambda key: (read.append(key) or {
        "points": 6420, "unit": "credits", "plan": "Token Plan",
        "detail": "remains=6420", "error": ""})
    _install_upstream(module, monkeypatch, status=200)

    with _client(module) as client:
        refreshed = client.post("/admin/pool/points")
        assert refreshed.status_code == 200
        account = refreshed.json()["account_pool"]["accounts"][0]
        assert account["points"] == 6420
        assert account["points_unit"] == "credits"
        assert read == ["sk-mm-real"], "the TTL is not a reason to skip a forced read"
        # the panel reads them off /health, not through a second reader
        assert client.get("/health").json()["account_pool"]["accounts"][0]["points"] == 6420

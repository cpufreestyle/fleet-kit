"""The Kimi bridge must tell a lapsed plan apart from a dead key.

Measured 2026-10-03 against the real upstream: /v1/models answers 200 with the
key from the Kimi desktop app's own storage, while /v1/chat/completions answers
403 access_terminated_error because that account has no active plan. Reporting
that as a refused key sends the operator hunting for a credential that is
perfectly fine, so the route names the plan and the renewal page instead.

The second thing pinned here is the account pool (bridges/plan_key_pool.py):
the bridge now holds one key per account, so a 401 on one key has to move to
the next account instead of taking the node down, and a second key has to be
addable while the service is running. The generic pool has its own tests; what
is pinned here is the Kimi-specific wiring -- seeding, the 403 vocabulary, and
the admin endpoints a operator actually calls.

The third is where the first key comes from: the Kimi desktop app's own key
file, read-only. That fallback must stay opt-out-able and must never become
the local bridge key.
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

PATH = os.path.join(BRIDGES, "kimi", "kimi_bridge.py")
_spec = importlib.util.spec_from_file_location("kimi_bridge_ut", PATH)

UPSTREAM_CATALOG = {"object": "list", "data": [
    {"id": "kimi-for-coding", "object": "model"},
    {"id": "kimi-for-coding-highspeed", "object": "model"},
    {"id": "kimi-for-coding-tts", "object": "model"},
    {"id": "kimi-image-edit", "object": "model"},
]}
STATIC_CATALOG = ["kimi/kimi-for-coding", "kimi/kimi-for-coding-highspeed",
                  "kimi/k3", "kimi/k3-256k"]


def _load(monkeypatch, tmp_path, **env):
    """A private copy of the bridge, its pool rooted in tmp_path.

    The env patch is scoped to the import on purpose: the pool directory and
    the keys are read once, while the module is being built. A pool dir outside
    tmp_path would put keys on the developer's own machine.
    """
    settings = {"KIMI_AUTH_POOL_DIR": str(tmp_path / "auths"),
                "KIMI_NO_APP_KEY": "1"}
    for key in ("KIMI_CODING_API_KEY", "KIMI_CODING_API_KEYS", "KIMI2CODEX_KEY",
                "KIMI_UPSTREAM_KEY_FILE"):
        monkeypatch.delenv(key, raising=False)
    settings.update(env)
    for key, value in settings.items():
        monkeypatch.setenv(key, value)
    module = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(module)
    return module


def _install_upstream(module, monkeypatch, *, status=200, body=None, boom=None,
                      keys=None, seen=None):
    """MockTransport answers /models and records which key was offered."""
    def handler(request):
        key = (request.headers.get("authorization") or "")
        if seen is not None:
            seen.append(key)
        if boom is not None:
            raise boom
        # one answer per key: keys maps a key to what it answers, anything not
        # in it (and the whole map when there is none) gets the canned status
        answer = keys.get(key, 200) if keys is not None else status
        if answer == 200:
            return httpx.Response(200, json=UPSTREAM_CATALOG)
        return httpx.Response(answer, json=body or {"error": {"message": "nope"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(module, "client", lambda: client)


def _client(module):
    from fastapi.testclient import TestClient
    return TestClient(module.app)


# ---------------- what the caller sees ----------------

def test_a_refused_key_is_reported_not_masked(monkeypatch, tmp_path):
    module = _load(monkeypatch, tmp_path, KIMI_CODING_API_KEY="sk-kimi-real")
    _install_upstream(module, monkeypatch, status=401)

    r = _client(module).get("/v1/models")

    assert r.status_code == 401
    assert r.json()["error"]["type"] == "upstream_auth_error"
    assert "KIMI_CODING_API_KEY" in r.json()["error"]["message"]
    assert "admin/pool/add" in r.json()["error"]["message"]
    assert "data" not in r.json()


def test_a_healthy_upstream_wins_and_junk_is_filtered(monkeypatch, tmp_path):
    module = _load(monkeypatch, tmp_path, KIMI_CODING_API_KEY="sk-kimi-real")
    _install_upstream(module, monkeypatch, status=200)

    r = _client(module).get("/v1/models")

    ids = [m["id"] for m in r.json()["data"]]
    # tts and image-edit rows cannot answer chat/completions; hiding them keeps
    # the picker honest
    assert ids == ["kimi/kimi-for-coding", "kimi/kimi-for-coding-highspeed"]
    assert "_fallback" not in r.json()


def test_no_key_still_serves_the_static_catalog(monkeypatch, tmp_path):
    module = _load(monkeypatch, tmp_path)

    r = _client(module).get("/v1/models")

    assert r.status_code == 200
    assert [m["id"] for m in r.json()["data"]] == STATIC_CATALOG
    assert "_fallback" not in r.json()
    assert r.json()["data"][0]["owned_by"] == "kimi-code"


def test_an_unreachable_upstream_keeps_the_catalog_and_names_the_layer(
        monkeypatch, tmp_path):
    module = _load(monkeypatch, tmp_path, KIMI_CODING_API_KEY="sk-kimi-real")
    _install_upstream(module, monkeypatch, boom=httpx.ConnectError("no route"))

    r = _client(module).get("/v1/models")

    # a dead network is not the account's fault: the measured catalog still
    # beats an error page, and the row says which layer broke
    assert r.status_code == 200
    assert [m["id"] for m in r.json()["data"]] == STATIC_CATALOG
    assert "ConnectError" in r.json()["_fallback"]


def test_a_lapsed_plan_is_not_reported_as_a_dead_key(monkeypatch, tmp_path):
    """The one case this bridge exists to phrase correctly."""
    module = _load(monkeypatch, tmp_path, KIMI_CODING_API_KEY="sk-kimi-real")
    _install_upstream(module, monkeypatch, status=403, body={
        "error": {"type": "access_terminated_error",
                  "message": "Your current subscription does not have access "
                             "to Kimi Code right now. Upgrade your plan."}})

    r = _client(module).post("/v1/chat/completions",
                             json={"model": "kimi/kimi-for-coding",
                                   "messages": [{"role": "user", "content": "hi"}]})

    assert r.status_code == 403
    err = r.json()["error"]
    assert err["type"] == "kimi_plan_inactive"
    assert "kimi.com/code/#pricing" in err["message"]
    # the key itself is fine, and the reply has to say so
    assert "the key itself is accepted" in err["message"]


def test_a_plain_403_keeps_the_generic_error(monkeypatch, tmp_path):
    module = _load(monkeypatch, tmp_path, KIMI_CODING_API_KEY="sk-kimi-real")
    _install_upstream(module, monkeypatch, status=403, body={
        "error": {"type": "rate_limit_error", "message": "slow down"}})

    r = _client(module).post("/v1/chat/completions",
                             json={"model": "kimi/kimi-for-coding",
                                   "messages": [{"role": "user", "content": "hi"}]})

    assert r.status_code == 403
    assert r.json()["error"]["type"] == "kimi_upstream_error"


def test_chat_without_any_key_names_both_ways_to_supply_one(monkeypatch, tmp_path):
    module = _load(monkeypatch, tmp_path)
    seen = []
    _install_upstream(module, monkeypatch, status=200, seen=seen)

    r = _client(module).post("/v1/chat/completions",
                             json={"model": "kimi/kimi-for-coding",
                                   "messages": [{"role": "user", "content": "hi"}]})

    assert r.status_code == 503
    message = r.json()["error"]["message"]
    assert "KIMI_CODING_API_KEY" in message
    assert "Kimi desktop app" in message
    assert seen == [], "no upstream call is worth making without a key"


# ---------------- the pool ----------------

def test_a_refused_key_fails_over_to_the_next_account(monkeypatch, tmp_path):
    module = _load(monkeypatch, tmp_path,
                   KIMI_CODING_API_KEY="sk-kimi-first",
                   KIMI_CODING_API_KEYS="sk-kimi-second")
    seen = []
    _install_upstream(module, monkeypatch,
                      keys={"Bearer sk-kimi-first": 401}, seen=seen)

    r = _client(module).get("/v1/models")

    assert r.status_code == 200
    assert [m["id"] for m in r.json()["data"]] == \
        ["kimi/kimi-for-coding", "kimi/kimi-for-coding-highspeed"]
    assert seen == ["Bearer sk-kimi-first", "Bearer sk-kimi-second"]
    # the key that failed is cooling, and /health says so
    health = _client(module).get("/health").json()
    states = {item["key_tail"]: item["state"]
              for item in health["account_pool"]["accounts"]}
    assert states == {"irst": "cooling", "cond": "ready"}
    assert health["account_pool"]["count"] == 2


def test_a_second_key_can_be_added_while_the_service_is_running(monkeypatch, tmp_path):
    module = _load(monkeypatch, tmp_path, KIMI_CODING_API_KEY="sk-kimi-first")
    seen = []
    _install_upstream(module, monkeypatch, keys={"Bearer sk-kimi-first": 401}, seen=seen)

    with _client(module) as client:
        added = client.post("/admin/pool/add", json={"key": "sk-kimi-second"})
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
        assert sources == {"irst": "env KIMI_CODING_API_KEY", "cond": "admin"}


def test_points_reach_health_for_the_panel(monkeypatch, tmp_path):
    module = _load(monkeypatch, tmp_path, KIMI_CODING_API_KEY="sk-kimi-real")
    read = []
    def _reader(row):
        read.append(row["key"])
        return {"points": 6420, "unit": "credits", "plan": "Kimi Code",
                "detail": "remains=6420", "error": ""}

    module.POOL.set_points_reader(_reader)
    _install_upstream(module, monkeypatch, status=200)

    with _client(module) as client:
        refreshed = client.post("/admin/pool/points")
        assert refreshed.status_code == 200
        account = refreshed.json()["account_pool"]["accounts"][0]
        assert account["points"] == 6420
        assert account["points_unit"] == "credits"
        assert read == ["sk-kimi-real"], "the TTL is not a reason to skip a forced read"
        # the panel reads them off /health, not through a second reader
        assert client.get("/health").json()["account_pool"]["accounts"][0]["points"] == 6420


# ---------------- where the first key comes from ----------------

def test_the_local_bridge_key_is_not_reused_upstream(monkeypatch, tmp_path):
    """A loopback token must never be shipped to a third party."""
    module = _load(monkeypatch, tmp_path, KIMI2CODEX_KEY="sk-local-bridge-key")
    seen = []
    _install_upstream(module, monkeypatch, status=200, seen=seen)

    # the bridge has no upstream key at all with the app fallback disabled
    assert module.POOL.status() == []
    r = _client(module).get("/health")
    assert r.json()["has_api_key"] is False
    assert r.json()["key_source"] == "\u65e0 key"
    assert seen == []


def test_the_desktop_app_key_file_is_read_as_a_fallback(monkeypatch, tmp_path):
    """The key already on this machine is the whole point of the fallback."""
    path = tmp_path / "daimon-share" / "daimon" / "kimi-code-key.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"v": 2, "keys": [
        {"userId": "u1", "apiKey": "sk-kimi-from-app", "keyId": "k"}]}),
        encoding="utf-8")
    # a relative path keeps the seeded source under the pool's 120-char cut
    monkeypatch.chdir(tmp_path)
    module = _load(monkeypatch, tmp_path,
                   KIMI_UPSTREAM_KEY_FILE="daimon-share/daimon/kimi-code-key.json",
                   KIMI_NO_APP_KEY="")

    accounts = module.POOL.status()
    assert len(accounts) == 1
    assert accounts[0]["source"] == "daimon-share/daimon/kimi-code-key.json"
    assert accounts[0]["key_tail"] == "-app"
    r = _client(module).get("/health")
    assert r.json()["has_api_key"] is True


def test_the_app_key_fallback_is_opt_out(monkeypatch, tmp_path):
    path = tmp_path / "kimi-code-key.json"
    path.write_text(json.dumps({"v": 2, "keys": [{"apiKey": "sk-kimi-from-app"}]}),
                    encoding="utf-8")
    module = _load(monkeypatch, tmp_path, KIMI_UPSTREAM_KEY_FILE=str(path))

    assert module.POOL.status() == []


def test_a_broken_app_key_file_says_why(monkeypatch, tmp_path):
    path = tmp_path / "kimi-code-key.json"
    path.write_text("{not json", encoding="utf-8")
    module = _load(monkeypatch, tmp_path, KIMI_UPSTREAM_KEY_FILE=str(path),
                   KIMI_NO_APP_KEY="")

    assert module.POOL.status() == []
    key, where = module.key_from_app()
    assert key is None
    assert "JSONDecodeError" in where


def test_an_env_key_wins_over_the_app_file(monkeypatch, tmp_path):
    path = tmp_path / "kimi-code-key.json"
    path.write_text(json.dumps({"v": 2, "keys": [{"apiKey": "sk-kimi-from-app"}]}),
                    encoding="utf-8")
    module = _load(monkeypatch, tmp_path, KIMI_UPSTREAM_KEY_FILE=str(path),
                   KIMI_CODING_API_KEY="sk-kimi-from-env")

    sources = [item["source"] for item in module.POOL.status()]
    assert sources == ["env KIMI_CODING_API_KEY"]

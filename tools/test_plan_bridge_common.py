"""Shared scaffolding for the plan-key bridges (kimi, minimax).

Both bridges are the same shape -- one API key per account, a pool rooted in a
per-bridge directory, an upstream /models call that decides the catalog, and a
502 envelope that has to name which credential was refused -- so their tests
grew the same three helpers twice. What differs per bridge is spelled out here
as data: which env vars seed the pool, which directory holds it, what an
upstream failure body looks like.

load() re-imports the bridge for every call on purpose. The env patch is
scoped to the import because the pool directory and the keys are read once,
while the module is being built; a pool dir outside tmp_path would put keys on
the developer's own machine, and a module-level import would freeze whichever
env the first test set.
"""
import importlib.util
import json
import os

import httpx


def load(module_name, rel_path, monkeypatch, tmp_path, *, pool_dir_env,
         pool_dir_name="auths", extra_env=None, clear_env=(), env=None):
    """A private copy of the bridge, its pool rooted in tmp_path.

    `env` is the per-test environment the caller wants set on top of the
    defaults; it is a dict, not **kwargs, so an env var may share a name with
    one of the keyword arguments above.
    """
    settings = {pool_dir_env: str(tmp_path / pool_dir_name)}
    for key in clear_env:
        monkeypatch.delenv(key, raising=False)
    settings.update(extra_env or {})
    settings.update(env or {})
    for key, value in settings.items():
        monkeypatch.setenv(key, value)
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir,
                        "bridges", rel_path)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def install_upstream(module, monkeypatch, *, status=200, body=None, boom=None,
                     keys=None, seen=None, bodies=None, catalog=None):
    """MockTransport answers /models and records which key was offered."""
    def handler(request):
        auth = (request.headers.get("authorization") or "")
        if seen is not None:
            seen.append(auth)
        if bodies is not None:
            bodies.append(_decode(request.content))
        if boom is not None:
            raise boom
        # one answer per key: keys maps an authorization header to what it
        # answers, anything not in it (and the whole map when there is none)
        # gets the canned status
        answer = keys.get(auth, 200) if keys is not None else status
        if answer == 200:
            return httpx.Response(200, json=catalog)
        return httpx.Response(answer, json=body or {
            "error": {"type": "authorized_error",
                      "message": "login fail: Please carry the API secret key"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(module, "client", lambda: client)


def _decode(content):
    """The request body as a dict, or None when it is not JSON."""
    try:
        return json.loads(content or b"{}")
    except ValueError:
        return None


def client(module):
    from fastapi.testclient import TestClient
    return TestClient(module.app)


def assert_static_catalog(load, static_catalog, owned_by, monkeypatch, tmp_path):
    """A fresh install has no key, so the bridge's own rows are the picker.

    The assertion is the contract, not the data: /v1/models answers 200 with
    exactly the documented rows, carries no "_fallback" escape hatch, and tags
    the first row with the bridge's own owner so the panel can group by bridge
    rather than by upstream. Both plan bridges ship the same shape, so the
    shared spelling here keeps one of them from quietly dropping a clause.
    """
    module = load(monkeypatch, tmp_path)

    r = client(module).get("/v1/models")

    assert r.status_code == 200
    assert [m["id"] for m in r.json()["data"]] == static_catalog
    assert "_fallback" not in r.json()
    assert r.json()["data"][0]["owned_by"] == owned_by

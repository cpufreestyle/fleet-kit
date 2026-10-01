"""A routine, unreachable upstream must never surface as a bare 500.

Measured 2026-09-30 on codely: GET /v1/models answered 500 Internal Server
Error six times in a row, codely.log held 291KB of httpx.ConnectError
tracebacks, and the status panel reported the bridge down with 0 models while
the catalog was fine and only the company network was down. The route already
had a complete degradation path -- a static catalog plus an
X-Codely-Models-Fallback header -- that one missing "except Exception" made
unreachable.

Fixing that one handler is not enough: nine modules in this tree build a
FastAPI app, and any of them can forget. So _common.make_app now installs a
handler for httpx.HTTPError, and the third test below makes sure no bridge can
quietly opt out of it.
"""
import io
import os
import re
import sys

import httpx
import pytest

BRIDGES = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    os.pardir, "bridges"))
sys.path.insert(0, BRIDGES)
import _common


def _app_that_calls_an_unreachable_upstream():
    app = _common.make_app("guard-test")

    def boom(request):
        raise httpx.ConnectError("All connection attempts failed")

    client = httpx.AsyncClient(transport=httpx.MockTransport(boom))

    @app.get("/v1/chat/completions")
    async def chat():
        return await client.get("http://codely-litellm.tuanjie.cn/v1/models")

    @app.get("/v1/models")
    async def models():
        return await client.get("http://codely-litellm.tuanjie.cn/v1/models")

    return app


def test_unreachable_upstream_becomes_503_not_500():
    from fastapi.testclient import TestClient

    c = TestClient(_app_that_calls_an_unreachable_upstream())
    for path in ("/v1/models", "/v1/chat/completions"):
        r = c.get(path)
        assert r.status_code == 503, path
        err = r.json()["error"]
        assert "upstream unreachable" in err["message"], path
        assert "ConnectError" in err["message"], path
        assert err["type"] == "upstream_unreachable", path


def test_the_guard_leaves_the_route_own_decisions_alone():
    """A class-registered handler only runs when nothing caught it.

    So the bridge's own 4xx must keep its status and wording, and a genuine
    bug must keep escaping -- swallowing that would hide real defects behind a
    tidy 503.
    """
    from fastapi import HTTPException
    from fastapi.testclient import TestClient

    app = _common.make_app("guard-test")

    @app.get("/mine")
    async def mine():
        raise HTTPException(status_code=400, detail="mine")

    @app.get("/real-bug")
    async def real_bug():
        raise ValueError("a genuine bug")

    c = TestClient(app)
    assert c.get("/mine").status_code == 400
    assert c.get("/mine").json()["detail"] == "mine"
    with pytest.raises(ValueError):
        c.get("/real-bug")


def _bridge_modules_that_build_an_app():
    found = []
    for root, dirs, files in os.walk(BRIDGES):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in sorted(files):
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            with io.open(path, encoding="utf-8", errors="replace") as fh:
                src = fh.read()
            if re.search(r"^app\s*=", src, re.MULTILINE):
                found.append((os.path.relpath(path, BRIDGES), src))
    return found


def test_every_bridge_app_is_built_through_common():
    """No bridge may build its own bare FastAPI() again.

    Two bridges did exactly that (workbuddy, cline) and both had the 500 hole;
    the floor of 9 keeps an empty or renamed scan from passing vacuously.
    """
    found = _bridge_modules_that_build_an_app()
    assert len(found) >= 9, [rel for rel, _ in found]
    offenders = [rel for rel, src in found
                 if "_common.make_app(" not in src
                 and "install_upstream_guard" not in src]
    assert offenders == [], "these bridges build an app without the guard: %s" % offenders

"""The xhx (raccoon) bridge must survive an account running out of points.

Measured 2026-10-03: the bridge held one account, read from the desktop app
auth.json, and an account out of points failed every request until the user
re-logged-in by hand. workbuddy solved this years-earlier-in-fleet-time with an
account pool (bridges/workbuddy/account_pool.py); these tests pin the ported
pool for xhx:

* the pool keeps its own copies and never writes the official login dir, which
  the desktop app clears at will
* insufficient_points cools the account and the next account answers
* a refresh rotates the single-use refresh_token into the pool file only
* points per account reach /health for the panel
"""

import asyncio
import base64
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

KIT_DIR = Path(__file__).resolve().parent.parent
BRIDGE_DIR = KIT_DIR / "bridges" / "xhx"


def _jwt(payload: dict) -> str:
    def part(obj):
        raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return part({"alg": "HS256", "typ": "JWT"}) + "." + part(payload) + ".sig"


def _session(name="RaccoonJames", sid="web-aaaa", exp=None, nation="86"):
    return {"access_token": _jwt({"name": name, "sid": sid, "nation_code": nation,
                                  "exp": exp if exp is not None else time.time() + 3600}),
            "refresh_token": _jwt({"sid": sid, "exp": time.time() + 7200}),
            "office_identity": "personal"}


class FakeResponse:
    def __init__(self, status=200, payload=None, raw=b""):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self._raw = raw or json.dumps(self._payload).encode("utf-8")
        self.closed = False

    def json(self):
        return self._payload

    async def aread(self):
        return self._raw

    async def aclose(self):
        self.closed = True


class FakeStream:
    def __init__(self, status=200, chunks=(), raw=b""):
        self.status_code = status
        self._chunks = list(chunks)
        self._raw = raw
        self.closed = False

    async def aread(self):
        return self._raw

    async def aclose(self):
        self.closed = True

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk


class FakeClient:
    """Answers /auth/v1/refresh and the balance endpoint from canned maps."""

    def __init__(self, refreshes=None, balances=None, streams=None, statuses=None):
        self.refreshes = refreshes or {}
        self.balances = balances or {}
        self.streams = streams or []
        self.statuses = statuses or []
        self.refresh_calls = []

    async def post(self, url, json=None, headers=None):
        rt = (json or {}).get("refresh_token") or ""
        self.refresh_calls.append(rt)
        outcome = self.refreshes.get(rt)
        if outcome is None:
            return FakeResponse(409, {"code": 200822, "message": "refresh_conflict"})
        status, payload = outcome
        return FakeResponse(status, payload)

    async def get(self, url, headers=None):
        bearer = (headers or {}).get("Authorization", "")
        outcome = self.balances.get(bearer)
        if outcome is None:
            return FakeResponse(401, {"code": 401, "message": "unauthorized"})
        status, payload = outcome
        return FakeResponse(status, payload)

    async def request(self, method, url, headers=None, **kw):
        bearer = (headers or {}).get("Authorization", "")
        status, payload = self.statuses.pop(0) if self.statuses else (200, {"data": {}})
        return FakeResponse(status, payload)

    def stream(self, method, url, headers=None, json=None):
        bearer = (headers or {}).get("Authorization", "")
        outcome = self.streams.pop(0) if self.streams else None
        if outcome is None:
            raise AssertionError("no canned stream left for " + bearer)
        return _StreamCtx(outcome)


class _StreamCtx:
    def __init__(self, stream):
        self._stream = stream

    async def __aenter__(self):
        return self._stream

    async def __aexit__(self, *exc):
        self._stream.closed = True
        return False


def _load_bridge(tmp: Path, official: Path, client_factory):
    """Load a private copy of the bridge with its pool rooted in tmp."""
    env = {"XHX_AUTH_POOL_DIR": str(tmp / "auths"),
           "BOX_AGENT_CONFIG_DIR": str(official),
           "XHX_WEB_BASE_URL": "https://raccoon.test",
           "XHX_POINTS_TTL": "300"}
    # The env patch is started and never stopped on purpose: auth_file(),
    # XHX_WEB_BASE_URL and the pool dir are read at *call* time (import_current,
    # reload -> brand-new sessions, refresh, points), not only at import. A
    # with-block that ends at exec_module would let those reads fall back to
    # the real ~/.box-agent/config/auth.json and xiaohuanxiong.com mid-test.
    patcher = mock.patch.dict(os.environ, env, clear=False)
    patcher.start()
    spec = importlib.util.spec_from_file_location("xhx_bridge_ut", BRIDGE_DIR / "xhx_bridge.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.client = lambda: client_factory
    module.POOL._client_factory = lambda: client_factory
    for account in list(module.POOL._managers.values()):
        account._client_factory = lambda: client_factory
    return module


class XhxAccountPoolTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.official = self.tmp / "official"
        self.official.mkdir()
        (self.official / "auth.json").write_text(
            json.dumps(_session(), ensure_ascii=False), encoding="utf-8")
        self.official_bytes = (self.official / "auth.json").read_bytes()
        self.client = FakeClient()

    def pool(self):
        module = _load_bridge(self.tmp, self.official, self.client)
        return module, module.POOL

    def test_import_copies_and_leaves_the_official_login_alone(self):
        module, pool = self.pool()
        accounts = pool.status()
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0]["name"], "RaccoonJames")
        self.assertEqual(accounts[0]["state"], "ready")
        self.assertTrue(accounts[0]["primary"])
        stored = list((self.tmp / "auths").glob("xhx-*.json"))
        self.assertEqual(len(stored), 1)
        # the pool file keeps the full account_uid; the panel view masks it
        self.assertEqual(json.loads(stored[0].read_text())["account_uid"], "web-aaaa")
        self.assertEqual(pool.status()[0]["uid"], "we**aa")
        # the official login is read, never written
        self.assertEqual((self.official / "auth.json").read_bytes(), self.official_bytes)
        self.assertEqual(oct((self.tmp / "auths").stat().st_mode & 0o777), "0o700")

    def test_second_account_is_appended_and_primary_kept(self):
        module, pool = self.pool()
        (self.official / "auth.json").write_text(
            json.dumps(_session(name="RaccoonKate", sid="web-bbbb"), ensure_ascii=False),
            encoding="utf-8")
        added = pool.import_current()
        self.assertEqual(len(pool.status()), 2)
        names = [item["name"] for item in pool.status()]
        self.assertIn("RaccoonKate", names)
        self.assertTrue(pool.status()[0]["primary"])
        self.assertEqual(pool.status()[0]["name"], "RaccoonJames")
        self.assertNotEqual(added["ref"], pool.status()[0]["ref"])
        self.assertEqual((self.official / "auth.json").read_bytes() != self.official_bytes, True)
        # the pool re-reads its own dir, so a cleared official login costs nothing
        (self.official / "auth.json").unlink()
        pool.reload()
        self.assertEqual(len(pool.status()), 2)

    def test_failure_cools_one_account_and_the_next_one_answers(self):
        module, pool = self.pool()
        (self.official / "auth.json").write_text(
            json.dumps(_session(name="RaccoonKate", sid="web-bbbb"), ensure_ascii=False),
            encoding="utf-8")
        pool.import_current()
        first, second = pool.candidates()
        self.assertEqual(first.manager.name, "RaccoonJames")
        # insufficient points on the primary account
        failure = module._account_failure(400, '{"code":"insufficient_points","message":"积分余额不足"}'.encode())
        self.assertEqual(failure, ("账号积分不足", 3600))
        pool.mark_failure(first.ref, failure[0], failure[1])
        remaining = pool.candidates()
        self.assertEqual([item.manager.name for item in remaining], ["RaccoonKate"])
        # the cooled account disappears from status() as cooling, not gone
        states = {item["name"]: item["state"] for item in pool.status()}
        self.assertEqual(states["RaccoonJames"], "cooling")
        # once the cooldown lapses it comes back
        with pool._lock:
            pool._state["accounts"][first.ref]["cooldown_until"] = time.time() - 1
        self.assertEqual(len(pool.candidates()), 2)

    def test_all_accounts_cooling_raises_pool_unavailable(self):
        module, pool = self.pool()
        only = pool.candidates()[0]
        pool.mark_failure(only.ref, "账号积分不足", 3600)
        self.assertEqual(pool.candidates(), [])
        async def call():
            return await module.upstream_request("GET", "/model_catalog")
        with self.assertRaises(Exception) as ctx:
            asyncio.run(call())
        self.assertEqual(getattr(ctx.exception, "status_code", None), 503)

    def test_stream_failover_happens_before_the_first_event(self):
        module, pool = self.pool()
        (self.official / "auth.json").write_text(
            json.dumps(_session(name="RaccoonKate", sid="web-bbbb"), ensure_ascii=False),
            encoding="utf-8")
        pool.import_current()
        first, second = pool.candidates()
        self.client.streams = [
            FakeStream(400, raw='{"code":"insufficient_points","message":"积分余额不足"}'.encode("utf-8")),
            FakeStream(200, chunks=[b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
                                    b"data: [DONE]\n\n"]),
        ]
        chunks = asyncio.run(_drain(module._stream_with_pool("/chat/completions", {"model": "x"})))
        joined = b"".join(chunks)
        self.assertIn(b"hi", joined)
        self.assertNotIn(b"insufficient_points", joined)
        self.assertEqual(json.loads((self.tmp / "auths" / "pool-state.json").read_text())["active_ref"],
                         second.ref)

    def test_refresh_rotates_into_the_pool_file_only(self):
        module, pool = self.pool()
        account = pool.status()[0]
        manager = pool.manager_for(account["ref"])
        fresh = _session(sid="web-aaaa", exp=time.time() + 7200)
        self.client.refreshes[manager._auth_section()["refresh_token"][:40] + "*"] = None  # no match by design
        # key the canned refresh on the exact token in the file
        stored = json.loads(next((self.tmp / "auths").glob("xhx-*.json")).read_text())
        self.client.refreshes[stored["refresh_token"]] = (
            200, {"data": {"access_token": fresh["access_token"],
                           "refresh_token": fresh["refresh_token"]}})
        self.assertTrue(asyncio.run(manager.refresh()))
        after = json.loads(next((self.tmp / "auths").glob("xhx-*.json")).read_text())
        self.assertEqual(after["access_token"], fresh["access_token"])
        self.assertEqual(after["account_uid"], stored["account_uid"])
        self.assertEqual((self.official / "auth.json").read_bytes(), self.official_bytes)
        # the rotated refresh token is gone: replaying it fails, and the
        # account is cooled instead of hammering the endpoint
        self.assertFalse(asyncio.run(manager.refresh()))
        # after the rotation the session is usable again: headers come back
        self.assertTrue(asyncio.run(manager.ensure_headers()))

    def test_points_reach_the_pool_state_for_the_panel(self):
        module, pool = self.pool()
        account = pool.status()[0]
        bearer = "Bearer " + json.loads(next((self.tmp / "auths").glob("xhx-*.json")).read_text())["access_token"]
        self.client.balances[bearer] = (200, {"data": {"available_points": 6300,
                                                        "daily_points": 100,
                                                        "reward_points": 6200}})
        summary = asyncio.run(pool.refresh_points(force=True))
        item = summary["accounts"][0]
        self.assertEqual(item["points"], 6300)
        self.assertEqual(item["daily_points"], 100)
        self.assertTrue(item["points_updated_at"])
        self.assertEqual((self.official / "auth.json").read_bytes(), self.official_bytes)
        health = asyncio.run(module.health())
        self.assertEqual(health["account_pool"]["count"], 1)
        self.assertEqual(health["account_pool"]["accounts"][0]["points"], 6300)


async def _drain(generator):
    out = []
    async for chunk in generator:
        out.append(chunk)
    return out


if __name__ == "__main__":
    unittest.main()

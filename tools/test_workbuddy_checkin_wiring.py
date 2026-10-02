"""The account-change claim sweep has to run, and must not leak an event loop.

Both defects were invisible to every check that existed:

1. 'the sweep call' sat *after* the return statement in
   /ui/accounts/import-current and /ui/accounts/remove, so importing or removing
   an account never started a claim sweep.  Nothing raised -- the feature was
   simply unreachable, exactly the kind of bug a health check cannot see.
2. Both sweeps built their loop with asyncio.new_event_loop().run_until_complete()
   and never closed it, so every sweep left one more selector -- with its
   socketpair and pipes -- alive until the process exited.

The same audit found two user-facing 503 details that read as mojibake (a UTF-8
string an earlier editor decoded as GBK); the last test keeps that from coming
back unnoticed.
"""
import asyncio
import importlib.util
import io
import os
import sys

import pytest

BRIDGE_DIR = os.path.abspath(os.path.join(
    os.path.dirname(__file__), os.pardir, "bridges", "workbuddy"))
sys.path.insert(0, BRIDGE_DIR)

spec = importlib.util.spec_from_file_location(
    "workbuddy_checkin_wiring", os.path.join(BRIDGE_DIR, "core.py"))
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)

CLAIM_RESULT = {"claimed_accounts": 2, "claimed_total": 40}


class _FakeLoop:
    """Records what the bridge did with the loop it opened."""

    def __init__(self):
        self.ran = None
        self.closed = False

    def run_until_complete(self, coro):
        self.ran = coro
        return dict(CLAIM_RESULT)

    def close(self):
        self.closed = True


async def _noop():
    return None


def test_run_coro_returns_the_result_and_closes_the_loop(monkeypatch):
    fake = _FakeLoop()
    coro = _noop()
    monkeypatch.setattr(core.asyncio, "new_event_loop", lambda: fake)
    try:
        assert core._run_coro(coro) == CLAIM_RESULT
    finally:
        coro.close()
    assert fake.ran is coro
    assert fake.closed, "loop left open -- one selector fd leaked per sweep"


class _FakePool(core.AccountPool):
    """A real AccountPool subclass so the bridge's own isinstance() guard holds."""

    def __init__(self):
        pass

    def import_current(self):
        return {"ref": "primary"}

    def remove(self, ref):
        return {"ref": ref}

    def summary(self):
        return {"count": 1}


class _Client:
    host = "127.0.0.1"


class _FakeRequest:
    client = _Client()
    cookies = {"workbuddy_bridge_session": core.DASHBOARD_SESSION}
    headers = {"content-type": "application/json"}

    async def json(self):
        return {"ref": "primary"}


@pytest.mark.parametrize("endpoint",
                         ["dashboard_import_current", "dashboard_remove_account"])
def test_account_change_starts_the_claim_sweep(endpoint, monkeypatch):
    monkeypatch.setitem(core.CONFIG, "pool", _FakePool())
    monkeypatch.setitem(core.CONFIG, "account_service", None)
    started = []
    monkeypatch.setattr(core, "_auto_checkin_background",
                        lambda: started.append(True))

    result = asyncio.run(getattr(core, endpoint)(_FakeRequest()))

    assert result["status"] == "ok"
    assert started == [True], "sweep call unreachable (dead code after return)"


def test_checkin_details_are_readable_chinese():
    """The 503 bodies an operator sees must not be mojibake."""
    source = io.open(os.path.join(BRIDGE_DIR, "core.py"), encoding="utf-8").read()
    assert "Buddy " + chr(0x9354) not in source
    assert "签到助手服务尚未初始化" in source

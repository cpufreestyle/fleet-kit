"""trae_renew trades the cached refresh token for a fresh one ahead of expiry.

Measured 2026-10-03 against the real bridge: the CN access token ran to
2026-10-09 while its refresh token ran to 2027-03-24, so one renewal bought
eight days and rotated the refresh token. These tests pin the conservative
contract behind that: renew only inside --days of expiry (or under --refresh),
and keep the old cache whenever ExchangeToken fails or answers with a token
that buys no extra time. A fake bridge stands in for the real server so the
network is never touched.
"""
import importlib.util
import json
import os
import time

DAYS_MS = 86_400_000

HERE = os.path.dirname(os.path.abspath(__file__))


def _load():
    spec = importlib.util.spec_from_file_location(
        "trae_renew", os.path.join(HERE, "trae_renew.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


trae_renew = _load()


class _FakeBridge:
    """Stands in for trae_bridge: load/save the store, refresh async."""

    CREDS_FILE = "/tmp/fake-trae-creds.json"

    def __init__(self, cred, fresh=None, raise_exc=None):
        self._cred = cred
        self._fresh = fresh
        self._raise = raise_exc
        self.saved = []

    def load_store(self):
        if self._cred is None:
            return {}
        return {"credential": dict(self._cred)}

    async def refresh_credential(self, cred):
        if self._raise is not None:
            raise self._raise
        return self._fresh

    def save_store(self, payload):
        self.saved.append(payload)


def _cred(days_left, with_refresh=True):
    cred = {
        "account": "User6781982309",
        "edition": "Trae CN",
        "access_token": "t-access",
        "expires_at_ms": time.time() * 1000 + days_left * DAYS_MS,
    }
    if with_refresh:
        cred["refresh_token"] = "t-refresh"
        cred["refresh_expires_at_ms"] = time.time() * 1000 + 180 * DAYS_MS
    return cred


def _install(monkeypatch, bridge):
    monkeypatch.setattr(trae_renew, "_load_bridge", lambda path: bridge)


def test_far_from_expiry_is_left_alone(monkeypatch, capsys):
    bridge = _FakeBridge(_cred(30))
    _install(monkeypatch, bridge)

    rc = trae_renew.main([])

    assert rc == 0
    assert bridge.saved == []
    out = capsys.readouterr().out
    assert "left alone" in out
    assert "renewed now" not in out


def test_close_to_expiry_renews_and_saves(monkeypatch, capsys):
    old = _cred(2)
    fresh = _cred(10)
    fresh["refresh_token"] = "t-refresh-new"
    bridge = _FakeBridge(old, fresh=fresh)
    _install(monkeypatch, bridge)

    rc = trae_renew.main([])

    assert rc == 0
    assert len(bridge.saved) == 1
    assert bridge.saved[0]["credential"] is fresh
    out = capsys.readouterr().out
    assert "renewed now" in out
    assert "User6781982309" in out


def test_refresh_flag_renews_whatever_the_dates_say(monkeypatch, capsys):
    bridge = _FakeBridge(_cred(30), fresh=_cred(38))
    _install(monkeypatch, bridge)

    rc = trae_renew.main(["--refresh"])

    assert rc == 0
    assert len(bridge.saved) == 1


def test_token_that_buys_no_time_keeps_the_old_cache(monkeypatch, capsys):
    bridge = _FakeBridge(_cred(3), fresh=_cred(3))
    _install(monkeypatch, bridge)

    rc = trae_renew.main(["--refresh"])

    assert rc == 1
    assert bridge.saved == []
    assert "buys no time" in capsys.readouterr().out


def test_exchange_failure_keeps_the_old_cache(monkeypatch, capsys):
    bridge = _FakeBridge(_cred(3), raise_exc=RuntimeError("412 token revoked"))
    _install(monkeypatch, bridge)

    rc = trae_renew.main(["--refresh"])

    assert rc == 1
    assert bridge.saved == []
    assert "412 token revoked" in capsys.readouterr().out


def test_no_cached_access_token_reports_first(monkeypatch, capsys):
    bridge = _FakeBridge(None)
    _install(monkeypatch, bridge)

    rc = trae_renew.main(["--refresh"])

    assert rc == 1
    assert bridge.saved == []
    assert "log in to Trae CN first" in capsys.readouterr().out


def test_close_to_expiry_without_refresh_token(monkeypatch, capsys):
    bridge = _FakeBridge(_cred(2, with_refresh=False))
    _install(monkeypatch, bridge)

    rc = trae_renew.main([])

    assert rc == 1
    assert bridge.saved == []
    assert "no refresh token to trade" in capsys.readouterr().out


def test_json_report_marks_renewal(monkeypatch, capsys):
    bridge = _FakeBridge(_cred(2), fresh=_cred(10))
    _install(monkeypatch, bridge)

    rc = trae_renew.main(["--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["renewed"] is True
    assert payload["access_days_left"] > 9

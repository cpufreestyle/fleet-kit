#!/usr/bin/env python3
"""Renew the Trae bridge's access token before it expires.

The trae bridge keeps its credential at ~/.trae2codex/creds.json and trades
the refresh token for a fresh one at
POST https://api.trae.cn/cloudide/api/v3/trae/oauth/ExchangeToken -- the
same contract the Trae IDE itself follows, reversed out of storage.json by
dsh-connect-trae. Measured 2026-10-03: the cached CN access token ran to
2026-10-09 while its refresh token ran to 2027-03-24, so one renewal bought
eight days and rotated the refresh token. No re-login anywhere, and the
IDE's own storage.json is left untouched.

The bridge does refresh on its own, but only once a request lands within
five minutes of expiry, which is exactly the moment a session is mid-flight.
This tool covers what that flow does not: renewing ahead of the date, and
reading both dates from one place.

Usage:
  trae_renew.py                 # report, renew when the access token has
                                # fewer than --days days left (default 7)
  trae_renew.py --refresh       # renew now, whatever the dates say
  trae_renew.py --json          # machine readable report
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_BRIDGE = HERE.parent / "bridges" / "trae" / "trae_bridge.py"
SECOND = 1000


def _load_bridge(path: Path):
    """The bridge module is loaded from its path: it is a server, not an import."""
    spec = importlib.util.spec_from_file_location("trae_bridge_renew", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _date(ms) -> str:
    if not ms:
        return "-"
    moment = datetime.datetime.fromtimestamp(float(ms) / SECOND, datetime.timezone.utc)
    return (moment + datetime.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M")


def _days_left(ms) -> float:
    return (float(ms) - time.time() * 1000) / 86_400_000 if ms else -1.0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bridge", default=str(DEFAULT_BRIDGE),
                        help="path to trae_bridge.py")
    parser.add_argument("--refresh", action="store_true",
                        help="renew now instead of only when it is close")
    parser.add_argument("--days", type=float, default=7.0,
                        help="renew when the access token has this many days left")
    parser.add_argument("--json", action="store_true", help="machine readable report")
    args = parser.parse_args(argv)

    bridge = _load_bridge(Path(args.bridge))
    store = bridge.load_store()
    cred = store.get("credential") or {}
    if not cred.get("access_token"):
        print("no cached credential at %s: log in to Trae CN first"
              % bridge.CREDS_FILE)
        return 1

    report = {
        "account": cred.get("account") or "",
        "edition": cred.get("edition") or "",
        "access_expires": _date(cred.get("expires_at_ms")),
        "access_days_left": round(_days_left(cred.get("expires_at_ms")), 2),
        "refresh_expires": _date(cred.get("refresh_expires_at_ms")),
        "refresh_days_left": round(_days_left(cred.get("refresh_expires_at_ms")), 2),
    }

    left = _days_left(cred.get("expires_at_ms"))
    renew = args.refresh or left < args.days
    if renew and not cred.get("refresh_token"):
        print("access token has %.1f day(s) left but no refresh token to trade"
              % left)
        return 1
    if renew:
        try:
            fresh = asyncio.run(bridge.refresh_credential(cred))
        except Exception as exc:  # network refused, token rotated elsewhere
            print("ExchangeToken failed: %s" % exc)
            return 1
        gained = _days_left(fresh.get("expires_at_ms"))
        if gained <= left:
            print("ExchangeToken answered but the new token buys no time "
                  "(%.1f -> %.1f days); the old cache was kept" % (left, gained))
            return 1
        bridge.save_store({"credential": fresh, "source": "trae_renew",
                           "updated": int(time.time())})
        report["access_expires"] = _date(fresh.get("expires_at_ms"))
        report["access_days_left"] = round(gained, 2)
        report["refresh_expires"] = _date(fresh.get("refresh_expires_at_ms"))
        report["refresh_days_left"] = round(_days_left(fresh.get("refresh_expires_at_ms")), 2)
        report["renewed"] = True
    else:
        report["renewed"] = False

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("%s (%s)" % (report["account"], report["edition"]))
        print("  access token : %s (%.1f day(s) left)"
              % (report["access_expires"], report["access_days_left"]))
        print("  refresh token: %s (%.1f day(s) left)"
              % (report["refresh_expires"], report["refresh_days_left"]))
        print("  %s" % ("renewed now" if report["renewed"]
                        else "left alone (not close enough to renew)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

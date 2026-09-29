"""Buddy 加油站 (daily check-in) for the local WorkBuddy Bridge.

The WorkBuddy desktop client exposes two endpoints under the same backend the
bridge already talks to (copilot.tencent.com). No device fingerprint, no captcha,
no nonce — only the account's access token is required, exactly like reading the
balance. This module wraps them so the bridge can show status and auto-claim for
every account in the local pool.

Endpoints (reversed from WorkBuddy's app.asar):
  POST /v2/billing/meter/checkin-activity-status  -> activity + per-account state
  POST /v2/billing/meter/daily-checkin            -> claim today's credits
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx


BACKEND = "https://copilot.tencent.com"
USER_AGENT = "workbuddy2codex"
CHECKIN_STATUS_URL = f"{BACKEND}/v2/billing/meter/checkin-activity-status"
CHECKIN_CLAIM_URL = f"{BACKEND}/v2/billing/meter/daily-checkin"

# Cache the activity-level metadata (theme/season) briefly so the dashboard does
# not re-fetch it for every account.
_ACTIVITY_CACHE: dict[str, Any] = {}
_ACTIVITY_CACHE_TTL = 30.0


@dataclass
class CheckinResult:
    ref: str
    name: str
    ok: bool
    today_checked_in: bool
    claimed: bool = False
    credit: float = 0.0
    streak_days: int = 0
    message: str = ""


class WorkBuddyCheckinService:
    """Per-account Buddy 加油站 status + claim, backed by the credential pool."""

    def __init__(self, pool: Any, *, transport: httpx.AsyncBaseTransport | None = None):
        self.pool = pool
        self.transport = transport

    def _client(self) -> httpx.AsyncClient:
        kwargs: dict[str, Any] = {"timeout": 20, "follow_redirects": True}
        if self.transport is not None:
            kwargs["transport"] = self.transport
        return httpx.AsyncClient(**kwargs)

    async def _status_for(self, ref: str) -> dict:
        manager = await asyncio.to_thread(self.pool.manager_for, ref)
        headers = await asyncio.to_thread(manager.get_headers)
        async with self._client() as client:
            response = await client.post(CHECKIN_STATUS_URL, json={}, headers=headers)
            response.raise_for_status()
            payload = response.json()
        if payload.get("code") != 0 or not isinstance(payload.get("data"), dict):
            raise RuntimeError(str(payload.get("msg") or "签到状态读取失败"))
        return payload["data"]

    async def status(self, *, force: bool = False) -> dict:
        """Return activity metadata + a per-account check-in summary."""
        pool_summary = await asyncio.to_thread(self.pool.summary)
        accounts = pool_summary.get("accounts") or []
        results = await asyncio.gather(
            *[self._account_status(account["ref"]) for account in accounts],
            return_exceptions=True,
        )
        per_account = []
        any_unclaimed = False
        for account, result in zip(accounts, results):
            if isinstance(result, Exception):
                per_account.append({
                    "ref": account["ref"],
                    "name": account.get("name", "WorkBuddy account"),
                    "ok": False,
                    "today_checked_in": False,
                    "claimed": False,
                    "credit": 0,
                    "streak_days": 0,
                    "message": "状态读取失败",
                })
                continue
            per_account.append(result)
            if not result["today_checked_in"]:
                any_unclaimed = True
        activity = await self._activity_meta(force=force)
        return {
            "ok": True,
            "activity": activity,
            "accounts": per_account,
            "unclaimed_count": sum(1 for a in per_account if a.get("ok") and not a.get("today_checked_in")),
            "any_unclaimed": any_unclaimed,
        }

    async def _account_status(self, ref: str) -> dict:
        account = next(
            (a for a in (await asyncio.to_thread(self.pool.summary)).get("accounts") or [] if a["ref"] == ref),
            {"name": "WorkBuddy account"},
        )
        data = await self._status_for(ref)
        return {
            "ref": ref,
            "name": account.get("name", "WorkBuddy account"),
            "ok": True,
            "today_checked_in": bool(data.get("today_checked_in")),
            "claimed": bool(data.get("today_checked_in")),
            "credit": float(data.get("today_credit") or 0),
            "streak_days": int(data.get("streak_days") or 0),
            "message": "",
        }

    async def _activity_meta(self, *, force: bool = False) -> dict:
        cached = _ACTIVITY_CACHE.get("meta")
        import time
        if not force and cached and (time.time() - cached[0]) < _ACTIVITY_CACHE_TTL:
            return cached[1]
        # Activity metadata is identical across accounts; read from the first one.
        pool_summary = await asyncio.to_thread(self.pool.summary)
        accounts = pool_summary.get("accounts") or []
        meta: dict[str, Any] = {
            "active": False,
            "theme_name": "Buddy加油站",
            "activity_name": "",
            "season": None,
            "daily_credit": 0,
            "available": False,
        }
        if accounts:
            try:
                data = await self._status_for(accounts[0]["ref"])
                meta.update({
                    "active": bool(data.get("active")),
                    "theme_name": data.get("theme_name") or "Buddy加油站",
                    "activity_name": data.get("activity_name") or "",
                    "season": data.get("season"),
                    "daily_credit": float(data.get("daily_credit") or 0),
                    "available": True,
                })
            except Exception:
                meta["available"] = False
        if not meta["available"]:
            cached = _ACTIVITY_CACHE.get("meta")
            if cached: return cached[1]
        _ACTIVITY_CACHE["meta"] = (time.time(), meta)
        return meta

    async def claim_all(self) -> dict:
        """Claim today's check-in for every account that has not claimed yet."""
        pool_summary = await asyncio.to_thread(self.pool.summary)
        accounts = pool_summary.get("accounts") or []
        results = []
        claimed_total = 0.0
        for account in accounts:
            ref = account["ref"]
            name = account.get("name", "WorkBuddy account")
            try:
                before = await self._status_for(ref)
                if before.get("today_checked_in"):
                    results.append(CheckinResult(ref, name, True, True, False, 0.0,
                                                int(before.get("streak_days") or 0),
                                                "今日已领取").__dict__)
                    continue
                manager = await asyncio.to_thread(self.pool.manager_for, ref)
                headers = await asyncio.to_thread(manager.get_headers)
                async with self._client() as client:
                    response = await client.post(CHECKIN_CLAIM_URL, json={}, headers=headers)
                    response.raise_for_status()
                    payload = response.json()
                if payload.get("code") != 0 or not isinstance(payload.get("data"), dict):
                    results.append(CheckinResult(ref, name, False, False, False, 0.0, 0,
                                                str(payload.get("msg") or "领取失败")).__dict__)
                    continue
                credit = float(payload["data"].get("credit") or 0)
                streak = int(payload["data"].get("streak_days") or 0)
                claimed_total += credit
                results.append(CheckinResult(ref, name, True, True, True, credit, streak,
                                            f"领取成功 +{credit:.0f}").__dict__)
            except Exception as exc:
                results.append(CheckinResult(ref, name, False, False, False, 0.0, 0,
                                            str(exc)[:120]).__dict__)
        return {
            "ok": True,
            "accounts": results,
            "claimed_accounts": sum(1 for r in results if r.get("claimed")),
            "claimed_total": round(claimed_total, 2),
        }

    async def claim_one(self, ref: str) -> dict:
        """Claim today's check-in for a single account reference."""
        pool_summary = await asyncio.to_thread(self.pool.summary)
        account = next(
            (a for a in pool_summary.get("accounts") or [] if a["ref"] == ref), None
        )
        if account is None:
            return {"ok": False, "message": "账号不存在"}
        name = account.get("name", "WorkBuddy account")
        try:
            before = await self._status_for(ref)
            if before.get("today_checked_in"):
                return CheckinResult(ref, name, True, True, False, 0.0,
                                     int(before.get("streak_days") or 0), "今日已领取").__dict__
            manager = await asyncio.to_thread(self.pool.manager_for, ref)
            headers = await asyncio.to_thread(manager.get_headers)
            async with self._client() as client:
                response = await client.post(CHECKIN_CLAIM_URL, json={}, headers=headers)
                response.raise_for_status()
                payload = response.json()
            if payload.get("code") != 0 or not isinstance(payload.get("data"), dict):
                return CheckinResult(ref, name, False, False, False, 0.0, 0,
                                     str(payload.get("msg") or "领取失败")).__dict__
            credit = float(payload["data"].get("credit") or 0)
            streak = int(payload["data"].get("streak_days") or 0)
            return CheckinResult(ref, name, True, True, True, credit, streak,
                                 f"领取成功 +{credit:.0f}").__dict__
        except Exception as exc:
            return CheckinResult(ref, name, False, False, False, 0.0, 0, str(exc)[:120]).__dict__

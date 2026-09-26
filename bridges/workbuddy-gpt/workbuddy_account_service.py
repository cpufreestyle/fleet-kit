"""WorkBuddy login polling and per-account credit visibility for the local UI."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
import time
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import uuid

import httpx


LOGIN_PENDING_CODE = 11217
LOGIN_TIMEOUT_SECONDS = 300
LOGIN_POLL_INTERVAL_SECONDS = 1
QUOTA_CACHE_SECONDS = 180
BACKEND = "https://copilot.tencent.com"
WORKBUDDY_LOGIN_HOSTS = {"copilot.tencent.com", "www.codebuddy.cn", "codebuddy.cn"}
USER_AGENT = "workbuddy2codex"

_PACKAGE_LABELS = {
    "TCACA_code_001_PqouKr6QWV": "免费额度",
    "TCACA_code_002_AkiJS3ZHF5": "Pro 月度",
    "TCACA_code_003_FAnt7lcmRT": "Pro 年度",
    "TCACA_code_005_maRGyrHhw1": "Pro 月度",
    "TCACA_code_006_DbXS0lrypC": "Pro 试用",
    "TCACA_code_007_nzdH5h4Nl0": "活动额度",
    "TCACA_code_008_cfWoLwvjU4": "日常额度",
    "TCACA_code_009_0XmEQc2xOf": "积分包",
    "TCACA_code_023_4xbGhMrE6q": "Youth",
    "TCACA_code_026_BaESVICNoi": "Advanced",
    "TCACA_code_027_0FCGVA6vSa": "Flagship",
    "TCACA_code_028_NtpWi0jzXs": "活动奖励",
    "TCACA_code_029_6wCGEWquYy": "活动奖励",
    "TCACA_code_030_BjSt89qTvr": "活动奖励",
    "TCACA_code_038_OhvqZtiPKr": "积分包",
}


def _number(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def _time_value(value: Any) -> float:
    if not value:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value) / 1000 if float(value) > 10_000_000_000 else float(value)
    try:
        text = str(value).strip().replace(" ", "T", 1)
        parsed = datetime.fromisoformat(text)
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _iso_time(value: Any) -> str | None:
    stamp = _time_value(value)
    if not stamp:
        return None
    return datetime.fromtimestamp(stamp, timezone.utc).astimezone().isoformat(timespec="seconds")


def _data_body(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if isinstance(data, dict) and "data" in data:
        return data.get("data")
    return data


def _account_ref_from_session(session: dict) -> str:
    account = session.get("account") or {}
    uid = str(account.get("uid") or "").strip()
    if not uid:
        raise ValueError("WorkBuddy 登录结果缺少账号 ID")
    import hashlib
    return hashlib.sha256(uid.encode("utf-8")).hexdigest()[:16]


@dataclass
class LoginAttempt:
    login_id: str
    state: str
    auth_url: str
    expires_at: float
    login_session_id: str
    auth_token: dict | None = None
    account: dict | None = None
    account_retry_count: int = 0
    status: str = "waiting"
    error: str = ""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class WorkBuddyAccountService:
    """Owns short-lived login attempts and cached, non-secret account quotas."""

    def __init__(self, pool: Any, version: str, *, transport: httpx.AsyncBaseTransport | None = None):
        self.pool = pool
        self.version = version
        self.transport = transport
        self._attempts: dict[str, LoginAttempt] = {}
        self._quota_cache: dict[str, tuple[float, dict]] = {}
        self._lock = asyncio.Lock()

    def _client(self) -> httpx.AsyncClient:
        kwargs: dict[str, Any] = {"timeout": 20, "follow_redirects": True}
        if self.transport is not None:
            kwargs["transport"] = self.transport
        return httpx.AsyncClient(**kwargs)

    @staticmethod
    def _validate_auth_url(raw_url: str) -> None:
        parsed = urlsplit(raw_url)
        if parsed.scheme != "https" or parsed.hostname not in WORKBUDDY_LOGIN_HOSTS:
            raise RuntimeError("WorkBuddy 返回了不受信任的登录地址")

    def _decorate_login_url(self, raw_url: str, login_session_id: str) -> str:
        self._validate_auth_url(raw_url)
        parsed = urlsplit(raw_url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query["platform"] = "workbuddy"
        query["version"] = self.version
        query["loginSessionId"] = login_session_id
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))

    async def start_login(self) -> dict:
        async with self._client() as client:
            response = await client.post(
                f"{BACKEND}/v2/plugin/auth/state?platform=workbuddy",
                json={},
                headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if payload.get("code") != 0 or not isinstance(data, dict) or not data.get("state") or not data.get("authUrl"):
            raise RuntimeError(str(payload.get("msg") or "无法创建 WorkBuddy 登录会话"))
        login_id = uuid.uuid4().hex
        login_session_id = str(uuid.uuid4())
        attempt = LoginAttempt(
            login_id=login_id,
            state=str(data["state"]),
            auth_url=self._decorate_login_url(str(data["authUrl"]), login_session_id),
            expires_at=time.time() + LOGIN_TIMEOUT_SECONDS,
            login_session_id=login_session_id,
        )
        async with self._lock:
            self._attempts = {
                key: value for key, value in self._attempts.items()
                if value.expires_at > time.time() and value.status not in {"completed", "error"}
            }
            self._attempts[login_id] = attempt
        return {
            "login_id": login_id,
            "auth_url": attempt.auth_url,
            "expires_in": LOGIN_TIMEOUT_SECONDS,
        }

    async def login_status(self, login_id: str) -> dict:
        async with self._lock:
            attempt = self._attempts.get(login_id)
        if attempt is None:
            raise KeyError(login_id)
        if attempt.status in {"completed", "error"}:
            return self._attempt_payload(attempt)
        if time.time() >= attempt.expires_at:
            attempt.status = "expired"
            attempt.error = "登录会话已过期，请重新开始扫码"
            return self._attempt_payload(attempt)
        async with attempt.lock:
            if attempt.status in {"completed", "error"}:
                return self._attempt_payload(attempt)
            try:
                token = await self._fetch_auth_token(attempt)
                if token is None:
                    return self._attempt_payload(attempt)
                account = await self._fetch_account(attempt, token)
                if account is None:
                    return self._attempt_payload(attempt)
                token = self._normalize_auth(token)
                session = {"account": account, "auth": token}
                ref = _account_ref_from_session(session)
                added = await asyncio.to_thread(self.pool.add_session, session)
                attempt.auth_token = None
                attempt.account = None
                attempt.status = "completed"
                self._quota_cache.pop(ref, None)
                return {"status": "completed", "account": added}
            except Exception as exc:
                attempt.status = "error"
                attempt.error = str(exc)[:180]
                return self._attempt_payload(attempt)

    async def cancel_login(self, login_id: str) -> None:
        async with self._lock:
            attempt = self._attempts.pop(login_id, None)
        if attempt is not None:
            attempt.status = "cancelled"
            attempt.auth_token = None
            attempt.account = None

    async def _fetch_auth_token(self, attempt: LoginAttempt) -> dict | None:
        async with self._client() as client:
            response = await client.get(
                f"{BACKEND}/v2/plugin/auth/token",
                params={"state": attempt.state},
                headers={"Accept": "application/json", "User-Agent": USER_AGENT, "X-No-Authorization": "true"},
            )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") == LOGIN_PENDING_CODE:
            return None
        if payload.get("code") != 0 or not isinstance(payload.get("data"), dict):
            raise RuntimeError(str(payload.get("msg") or "WorkBuddy 登录未完成"))
        return payload["data"]

    async def _fetch_account(self, attempt: LoginAttempt, token: dict) -> dict | None:
        auth = str(token.get("accessToken") or "")
        if not auth:
            raise RuntimeError("登录结果缺少访问令牌")
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {auth}",
            "User-Agent": USER_AGENT,
            "X-Domain": str(token.get("domain") or "www.codebuddy.cn"),
            "X-No-User-Id": "true",
            "X-No-Enterprise-Id": "true",
            "X-No-Department-Info": "true",
        }
        async with self._client() as client:
            response = await client.get(
                f"{BACKEND}/v2/plugin/login/account",
                params={"state": attempt.state},
                headers=headers,
            )
        if response.status_code in {401, 403} and attempt.account_retry_count < 5:
            attempt.account_retry_count += 1
            return None
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") in {LOGIN_PENDING_CODE, 12151}:
            return None
        if payload.get("code") != 0 or not isinstance(_data_body(payload), dict):
            raise RuntimeError(str(payload.get("msg") or "无法读取 WorkBuddy 账号信息"))
        return _data_body(payload)

    @staticmethod
    def _normalize_auth(token: dict) -> dict:
        result = dict(token)
        now_ms = int(time.time() * 1000)
        if not result.get("expiresAt") and result.get("expiresIn"):
            result["expiresAt"] = now_ms + int(result["expiresIn"]) * 1000
        if not result.get("refreshExpiresAt") and result.get("refreshExpiresIn"):
            result["refreshExpiresAt"] = now_ms + int(result["refreshExpiresIn"]) * 1000
        result["lastRefreshTime"] = now_ms
        result["domain"] = result.get("domain") or "www.codebuddy.cn"
        return result

    @staticmethod
    def _attempt_payload(attempt: LoginAttempt) -> dict:
        result = {"status": attempt.status}
        if attempt.status == "waiting":
            result["expires_in"] = max(0, int(attempt.expires_at - time.time()))
        if attempt.error:
            result["message"] = attempt.error
        return result

    def clear_quotas(self) -> None:
        self._quota_cache.clear()

    async def enrich_pool(self, *, force: bool = False) -> dict:
        pool_summary = await asyncio.to_thread(self.pool.summary)
        accounts = pool_summary.get("accounts") or []
        results = await asyncio.gather(*[
            self._quota_for(account["ref"], force=force) for account in accounts
        ], return_exceptions=True)
        total_left = total = used = 0.0
        quota_ok = False
        for account, quota in zip(accounts, results):
            if isinstance(quota, Exception):
                quota = {"ok": False, "message": "额度读取失败"}
            account["quota"] = quota
            if quota.get("ok"):
                quota_ok = True
                if not quota.get("unlimited"):
                    total_left += _number(quota.get("balance"))
                    total += _number(quota.get("total"))
                    used += _number(quota.get("used"))
        pool_summary["quota"] = {
            "ok": quota_ok,
            "unlimited": any((a.get("quota") or {}).get("unlimited") for a in accounts),
            "balance": round(total_left, 2),
            "total": round(total, 2),
            "used": round(used, 2),
        }
        return pool_summary

    async def _quota_for(self, ref: str, *, force: bool) -> dict:
        cached = self._quota_cache.get(ref)
        if cached and not force and cached[0] > time.time():
            return dict(cached[1])
        manager = await asyncio.to_thread(self.pool.manager_for, ref)
        headers = await asyncio.to_thread(manager.get_headers)
        session = await asyncio.to_thread(manager.session_snapshot)
        account = session.get("account") or {}
        endpoint = f"{BACKEND}/v2/billing/meter/"
        async with self._client() as client:
            if account.get("enterpriseId"):
                response = await client.post(endpoint + "get-enterprise-user-usage", json={}, headers=headers)
                quota = self._parse_enterprise(response.json())
            else:
                response = await client.post(
                    endpoint + "get-user-resource",
                    json={"PageNumber": 1, "PageSize": 100, "ProductCode": "p_tcaca", "Status": [0, 3], "OnlyValidPeriod": True},
                    headers=headers,
                )
                quota = self._parse_personal(response.json())
        self._quota_cache[ref] = (time.time() + QUOTA_CACHE_SECONDS, quota)
        return dict(quota)

    @staticmethod
    def _parse_enterprise(payload: dict) -> dict:
        data = _data_body(payload) or {}
        if not isinstance(data, dict) or payload.get("code") not in {0, None}:
            return {"ok": False, "message": "企业额度暂不可读"}
        limit = _number(data.get("limitNum"))
        credit = _number(data.get("credit"))
        if data.get("limitNum") == -1:
            return {"ok": True, "unlimited": True, "label": "企业账号 · 不限量"}
        return {
            "ok": True, "unlimited": False, "balance": max(0, limit - credit),
            "total": limit, "used": min(limit, max(0, credit)),
            "label": "企业额度", "refresh_at": _iso_time(data.get("cycleResetTime")),
        }

    @staticmethod
    def _parse_personal(payload: dict) -> dict:
        if payload.get("code") not in {0, None}:
            return {"ok": False, "message": "个人积分暂不可读"}
        data = payload.get("data") if isinstance(payload, dict) else None
        accounts = (((data or {}).get("Response") or {}).get("Data") or {}).get("Accounts") or []
        if not isinstance(accounts, list):
            return {"ok": False, "message": "个人积分返回格式异常"}
        items = []
        for resource in accounts:
            if not isinstance(resource, dict):
                continue
            total = _number(resource.get("CycleCapacitySizePrecise") or resource.get("CycleCapacitySize"))
            balance = _number(resource.get("CycleCapacityRemainPrecise") or resource.get("CycleCapacityRemain"))
            if total <= 0 and balance <= 0:
                continue
            package = str(resource.get("PackageCode") or "")
            items.append({
                "label": _PACKAGE_LABELS.get(package, "积分额度"),
                "balance": round(max(0, balance), 2),
                "total": round(max(0, total), 2),
                "used": round(max(0, total - balance), 2),
                "refresh_at": _iso_time(resource.get("CycleEndTime")),
            })
        if not items:
            return {"ok": True, "unlimited": False, "balance": 0, "total": 0, "used": 0, "label": "暂无可用积分"}
        total = sum(item["total"] for item in items)
        balance = sum(item["balance"] for item in items)
        used = sum(item["used"] for item in items)
        refresh_times = [item["refresh_at"] for item in items if item.get("refresh_at")]
        return {
            "ok": True, "unlimited": False, "balance": round(balance, 2),
            "total": round(total, 2), "used": round(used, 2),
            "label": "个人积分", "refresh_at": min(refresh_times) if refresh_times else None,
            "items": items[:4],
        }

"""Local, credential-safe xhx (SenseTime Raccoon) account pool.

The pool owns copies of the raccoon session files under the bridge auths
directory. It never writes to the official login directory
(~/.box-agent/config/auth.json), which the desktop app rewrites or clears at
will -- a bridge that shares that file loses its login the moment the app
rotates its single-use refresh_token.

Ported from bridges/workbuddy/account_pool.py with the same contract:
candidates() gives per-request failover, mark_success/mark_failure give
bounded cooldown, status()/summary() are what /health and the panel show. The
xhx-specific parts are the JWT identity, the single-use refresh rotation, and
the per-account points balance.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

# The pool skeleton -- the state file, the lock, the cooldown bookkeeping, the
# primary/active/LRU order, mark_success/mark_failure and the points reader
# loop -- lives in _account_pool.py, shared with plan_key_pool, workbuddy and
# gemini. What stays here is what only xhx has: the JWT identity, the
# single-use refresh rotation and the token expiry.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))
import _account_pool
from _account_pool import POINTS_TTL, account_ref, atomic_json, iso_timestamp

#: uid -> the pool's stable 16-hex ref (the file name and the state key)
_account_ref = account_ref


def _mask_uid(uid: object) -> str:
    """A uid is not a secret, but it is an identifier: show less of it."""
    text = str(uid or "").strip()
    if len(text) <= 4:
        return "****"
    if len(text) <= 8:
        return text[:2] + "**" + text[-2:]
    return text[:4] + "****" + text[-4:]

ACCOUNT_GLOB = "xhx-*.json"

# How often one account balance is re-read for the panel (seconds). The
# balance endpoint is a read-only GET, but there is no reason to hit every
# account on each /health poll.
POINTS_TTL = float(os.environ.get("XHX_POINTS_TTL") or "300")

# Refresh the access_token this long before it expires (seconds). The desktop
# app may rotate the refresh_token at any time, so refreshing a little early
# keeps "request sent, only then noticed the token aged out" rare.
REFRESH_MARGIN = float(os.environ.get("XHX_REFRESH_MARGIN") or "120")


def _jwt_payload(token: str) -> dict:
    """Decode a JWT payload without verifying it (the vendor signs it)."""
    parts = str(token or "").split(".")
    if len(parts) < 2:
        return {}
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, TypeError, binascii.Error, UnicodeEncodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _session_identity(session: dict) -> tuple[str, str, str, float]:
    """Return (uid, name, nation_code, exp) for a raccoon session file.

    uid is the stable account key: the stored account_uid when present (it
    survives a token rotation that mints a new sid), otherwise the JWT sid.
    """
    if not isinstance(session, dict):
        raise ValueError("bad raccoon session shape")
    auth = session.get("auth") if isinstance(session.get("auth"), dict) else session
    token = str(auth.get("access_token") or "").strip()
    if not token:
        raise ValueError("raccoon session without access_token")
    payload = _jwt_payload(token)
    stored = str(session.get("account_uid") or "").strip()
    uid = stored or str(payload.get("sid") or payload.get("jti") or "").strip()
    if not uid:
        raise ValueError("raccoon session without a usable account id")
    name = str(payload.get("name") or session.get("name") or "xhx account").strip()
    nation = str(payload.get("nation_code") or "").strip()
    exp = float(payload.get("exp") or 0)
    return uid, name, nation, exp


class XhxAccountSession:
    """One raccoon account: its own session copy plus token upkeep."""

    def __init__(self, path: Path, client_factory: Callable[[], Any],
                 web_base: str = "") -> None:
        self.path = Path(path)
        self._client_factory = client_factory
        self._web_base = web_base or os.environ.get("XHX_WEB_BASE_URL") or "https://xiaohuanxiong.com"
        self._session = json.loads(self.path.read_text(encoding="utf-8"))
        uid, name, nation, exp = _session_identity(self._session)
        self.ref = _account_ref(uid)
        self.uid = uid
        self.name = name
        self.nation_code = nation
        self._exp = exp
        self._lock = threading.Lock()

    # ---------------- token ----------------

    @property
    def token_expires_at(self) -> float:
        return self._exp * 1000 if self._exp else 0.0

    @property
    def token_expired(self) -> bool:
        return bool(self._exp) and self._exp - REFRESH_MARGIN <= time.time()

    def summary(self) -> dict:
        return {
            "name": self.name,
            "uid": self.uid,
            "nation_code": self.nation_code,
            "token_expires_at": self.token_expires_at,
            "token_expired": self.token_expired,
        }

    def _auth_section(self) -> dict:
        auth = self._session.get("auth") if isinstance(self._session.get("auth"), dict) else self._session
        return auth if isinstance(auth, dict) else {}

    def headers(self) -> dict:
        return {"Authorization": "Bearer " + str(self._auth_section().get("access_token") or ""),
                "Content-Type": "application/json"}

    async def refresh(self) -> bool:
        """Single-use rotation; the new tokens land in this pool file only.

        Returns False when the refresh fails (a dead or already-rotated
        refresh_token), in which case the caller should cool the account down
        instead of hammering the same endpoint.
        """
        auth = self._auth_section()
        rt = str(auth.get("refresh_token") or "")
        if not rt:
            return False
        with self._lock:
            try:
                response = await self._client_factory().post(
                    self._web_base + "/api/web/auth/v1/refresh",
                    json={"refresh_token": rt},
                    headers={"Content-Type": "application/json"})
                if response.status_code != 200:
                    await response.aclose()
                    return False
                data = ((response.json() or {}).get("data") or {})
                await response.aclose()
                token = data.get("access_token")
                if not token:
                    return False
                new_rt = data.get("refresh_token") or rt
                merged = {**self._session, "account_uid": self.uid,
                          "access_token": token, "refresh_token": new_rt}
                if isinstance(self._session.get("auth"), dict):
                    merged["auth"] = {**self._session["auth"],
                                      "access_token": token, "refresh_token": new_rt}
                payload = _jwt_payload(token)
                if payload.get("exp"):
                    self._exp = float(payload["exp"])
                atomic_json(self.path, merged)
                self._session = merged
                return True
            except Exception:
                return False

    async def ensure_headers(self) -> dict | None:
        """Headers for one request, refreshing first when the token is stale."""
        if self.token_expired and not await self.refresh():
            return None
        return self.headers()

    # ---------------- points ----------------

    async def points(self) -> dict | None:
        """Read-only balance for this account, or None when unreadable."""
        headers = await self.ensure_headers()
        if headers is None:
            return None
        try:
            response = await self._client_factory().get(
                self._web_base + "/api/web/points/v1/balance", headers=headers)
            if response.status_code != 200:
                await response.aclose()
                return None
            data = (response.json() or {}).get("data") or {}
            await response.aclose()
            return data if isinstance(data, dict) else None
        except Exception:
            return None


class AccountCandidate(_account_pool.AccountCandidate):
    """One pickable raccoon session, with the manager a request goes through."""

    def __init__(self, ref: str, manager: Any) -> None:
        super().__init__(ref)
        self.manager = manager


class AccountPool(_account_pool.AccountPool):
    """Persisted preferred-account pool with bounded cooldown state.

    The skeleton comes from _account_pool.AccountPool; a raccoon *session* is
    the unit here, so scan() builds one XhxAccountSession per session file and
    the status row adds the token expiry the panel shows.
    """

    def __init__(
        self,
        auth_dir: Path,
        client_factory: Callable[[], Any],
        official_finder: Callable[[], Path | None],
        *,
        auto_import: bool = True,
        forbidden_dirs: list[Path] | None = None,
    ) -> None:
        self.auth_dir = Path(auth_dir).resolve()
        for forbidden in forbidden_dirs or []:
            resolved = Path(forbidden).resolve()
            if self.auth_dir == resolved or resolved in self.auth_dir.parents:
                raise ValueError("bridge auths dir must not be the official raccoon login dir or inside it")
        self._client_factory = client_factory
        self._official_finder = official_finder
        self.points_ttl = POINTS_TTL
        super().__init__(self.auth_dir)
        if auto_import and not self._rows:
            try:
                self.import_current()
            except (OSError, ValueError, RuntimeError):
                pass

    # ---------------- scanning ----------------

    def scan(self) -> list:
        """Every session file in auths/, as [{ref, session, path, name}]."""
        out = []
        for path in sorted(self.auth_dir.glob(ACCOUNT_GLOB)):
            try:
                session = XhxAccountSession(path, self._client_factory)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
            out.append({"ref": session.ref, "session": session, "path": path,
                        "name": _mask_uid(session.summary().get("uid"))})
        return out

    @property
    def _managers(self) -> dict:
        """ref -> the session manager, what a request is served through."""
        return {ref: row["session"] for ref, row in self._rows.items()}

    @property
    def _paths(self) -> dict:
        return {ref: row["path"] for ref, row in self._rows.items()}

    def import_current(self) -> dict:
        """Copy the desktop app current login into the pool (one-way)."""
        source = self._official_finder()
        if source is None or not Path(source).is_file():
            raise RuntimeError("no raccoon login found: sign in inside the SenseTime Raccoon desktop app")
        try:
            session = json.loads(Path(source).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("cannot read the raccoon login file") from exc
        return self.add_session(session)

    def add_session(self, session: dict) -> dict:
        """Store one authenticated raccoon session inside the isolated pool."""
        uid, _, _, _ = _session_identity(session)
        ref = _account_ref(uid)
        destination = self.auth_dir / ("xhx-" + ref + ".json")
        auth = session.get("auth") if isinstance(session.get("auth"), dict) else session
        payload = {**session,
                   "account_uid": uid,
                   "access_token": auth.get("access_token"),
                   "refresh_token": auth.get("refresh_token"),
                   "name": _jwt_payload(str(auth.get("access_token") or "")).get("name") or session.get("name") or ""}
        atomic_json(destination, payload)
        self.reload()
        with self._lock:
            state = self._state["accounts"].setdefault(ref, {})
            state.update({"cooldown_until": 0, "reason": "", "failures": 0})
            if not self._state.get("primary_ref"):
                self._state["primary_ref"] = ref
            self._save_locked()
        return self.get_account(ref)

    def manager_for(self, ref: str) -> XhxAccountSession:
        with self._lock:
            manager = self._managers.get(ref)
            if manager is None:
                raise KeyError(ref)
            return manager

    def get_account(self, ref: str) -> dict:
        for item in self.status():
            if item["ref"] == ref:
                return item
        raise KeyError(ref)

    def set_primary(self, ref: str) -> None:
        with self._lock:
            if ref not in self._managers:
                raise KeyError(ref)
            self._state["primary_ref"] = ref
            self._save_locked()

    def remove(self, ref: str) -> None:
        with self._lock:
            path = self._paths.get(ref)
            if path is None:
                raise KeyError(ref)
            resolved = path.resolve()
            if resolved.parent != self.auth_dir:
                raise RuntimeError("account file is not inside the local auths dir")
            resolved.unlink(missing_ok=True)
            self.reload()

    # ---------------- selection ----------------

    def candidates(self) -> list[AccountCandidate]:
        return [AccountCandidate(ref, self._managers[ref])
                for ref in self._ready_refs()]

    # ---------------- points ----------------

    # ---------------- views ----------------

    def read_points(self, row: dict):
        """One account's balance, straight from the async raccoon client.

        The reader is async here (the session owns the client), so
        refresh_points() awaits whatever comes back instead of assuming a
        worker thread.
        """
        return row["session"].points()

    def points_row(self, data: dict) -> dict:
        """The raccoon balance: one available figure, split into daily+reward."""
        row = {
            "points": data.get("available_points"),
            "points_error": "balance read failed" if not data else "",
            "daily_points": data.get("daily_points"),
            "reward_points": data.get("reward_points"),
        }
        row["points_ts"] = time.time()
        return row

    def account_row(self, row: dict, state: dict) -> dict:
        """One status row: the base fields plus the token expiry the panel shows."""
        entry = super().account_row(row, state)
        session = row["session"]
        try:
            summary = session.summary()
            entry["name"] = summary.get("name") or row.get("name")
            entry["uid"] = _mask_uid(summary.get("uid"))
            entry["nation_code"] = summary.get("nation_code") or ""
            entry["token_expires_at"] = iso_timestamp(float(summary.get("token_expires_at") or 0) / 1000)
            if summary.get("token_expired"):
                entry["state"] = "expired"
        except Exception:
            entry["name"] = row.get("name")
            entry["uid"] = "****"
            entry["state"] = "error"
            entry["reason"] = "credential unreadable"
            entry["points_error"] = ""
            entry["points_updated_at"] = None
            entry["token_expires_at"] = None
        return entry


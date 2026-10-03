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
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading
import time
from typing import Any, Callable


STATE_FILE_NAME = "pool-state.json"
ACCOUNT_GLOB = "xhx-*.json"

# How often one account balance is re-read for the panel (seconds). The
# balance endpoint is a read-only GET, but there is no reason to hit every
# account on each /health poll.
POINTS_TTL = float(os.environ.get("XHX_POINTS_TTL") or "300")

# Refresh the access_token this long before it expires (seconds). The desktop
# app may rotate the refresh_token at any time, so refreshing a little early
# keeps "request sent, only then noticed the token aged out" rare.
REFRESH_MARGIN = float(os.environ.get("XHX_REFRESH_MARGIN") or "120")


def _account_ref(uid: str) -> str:
    return hashlib.sha256(uid.encode("utf-8")).hexdigest()[:16]


def _mask_uid(uid: object) -> str:
    text = str(uid or "").strip()
    if len(text) <= 4:
        return "****"
    if len(text) <= 8:
        return text[:2] + "**" + text[-2:]
    return text[:4] + "****" + text[-4:]


def _iso_timestamp(value: float | int | None) -> str | None:
    if not value:
        return None
    return datetime.fromtimestamp(float(value), timezone.utc).astimezone().isoformat(timespec="seconds")


def harden_private_path(path: Path) -> None:
    """Restrict a credential directory/file to the current user and SYSTEM."""
    if os.name != "nt":
        os.chmod(path, 0o700 if path.is_dir() else 0o600)
        return
    try:
        identity = _current_identity()
        own_rule = identity + ":(OI)(CI)F" if path.is_dir() else identity + ":(F)"
        system_rule = "*S-1-5-18:(OI)(CI)F" if path.is_dir() else "*S-1-5-18:(F)"
        result = subprocess.run(
            [_system_tool("icacls.exe"), str(path), "/inheritance:r", "/grant:r",
             own_rule, system_rule],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=15, check=False,
        )
        if result.returncode != 0:
            raise OSError(result.stderr.strip() or result.stdout.strip() or "icacls failed")
    except (OSError, subprocess.SubprocessError) as exc:
        raise OSError("cannot protect credential path " + str(path)) from exc


def _system_tool(name: str) -> str:
    root = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or "C:\\Windows"
    candidate = os.path.join(root, "System32", name)
    return candidate if os.path.exists(candidate) else name


def _current_identity() -> str:
    whoami = _system_tool("whoami.exe")
    try:
        done = subprocess.run(
            [whoami, "/user"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=15, check=False,
        )
        for token in reversed((done.stdout or "").split()):
            if token.upper().startswith("S-1-"):
                # icacls needs the * prefix to resolve a raw SID
                return "*" + token
    except (OSError, subprocess.SubprocessError):
        pass
    identity = subprocess.check_output(
        [whoami], text=True, encoding="utf-8", errors="replace",
    ).strip()
    if not identity:
        raise OSError("cannot determine the current Windows user")
    return identity


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


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    harden_private_path(path.parent)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)
    harden_private_path(path)


@dataclass(frozen=True)
class AccountCandidate:
    ref: str
    manager: Any


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
                _atomic_json(self.path, merged)
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


class AccountPool:
    """Persisted preferred-account pool with bounded cooldown state."""

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
        self.state_path = self.auth_dir / STATE_FILE_NAME
        self._client_factory = client_factory
        self._official_finder = official_finder
        self._lock = threading.RLock()
        self._managers: dict[str, XhxAccountSession] = {}
        self._paths: dict[str, Path] = {}
        self._state: dict = {"primary_ref": None, "active_ref": None, "accounts": {}}
        self._points_lock = asyncio.Lock()
        self.auth_dir.mkdir(parents=True, exist_ok=True)
        harden_private_path(self.auth_dir)
        self._load_state()
        self.reload()
        if auto_import and not self._managers:
            try:
                self.import_current()
            except (OSError, ValueError, RuntimeError):
                pass

    # ---------------- persistence ----------------

    def _load_state(self) -> None:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                accounts = data.get("accounts")
                self._state = {
                    "primary_ref": data.get("primary_ref"),
                    "active_ref": data.get("active_ref"),
                    "accounts": accounts if isinstance(accounts, dict) else {},
                }
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return

    def _save_locked(self) -> None:
        _atomic_json(self.state_path, self._state)

    def reload(self) -> None:
        with self._lock:
            managers: dict[str, XhxAccountSession] = {}
            paths: dict[str, Path] = {}
            for path in sorted(self.auth_dir.glob(ACCOUNT_GLOB)):
                try:
                    account = XhxAccountSession(path, self._client_factory)
                    managers[account.ref] = account
                    paths[account.ref] = path
                    self._state["accounts"].setdefault(account.ref, {})
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    continue
            self._managers = managers
            self._paths = paths
            valid = set(managers)
            self._state["accounts"] = {
                ref: value for ref, value in self._state["accounts"].items()
                if ref in valid and isinstance(value, dict)
            }
            if self._state.get("primary_ref") not in valid:
                self._state["primary_ref"] = next(iter(managers), None)
            if self._state.get("active_ref") not in valid:
                self._state["active_ref"] = None
            self._save_locked()

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
        _atomic_json(destination, payload)
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
            self._state["accounts"].pop(ref, None)
            if self._state.get("primary_ref") == ref:
                self._state["primary_ref"] = None
            if self._state.get("active_ref") == ref:
                self._state["active_ref"] = None
            self.reload()

    # ---------------- selection ----------------

    def candidates(self) -> list[AccountCandidate]:
        now = time.time()
        with self._lock:
            healthy = [
                ref for ref in self._managers
                if float(self._state["accounts"].get(ref, {}).get("cooldown_until") or 0) <= now
            ]
            primary = self._state.get("primary_ref")
            active = self._state.get("active_ref")

            def rank(ref: str) -> tuple:
                state = self._state["accounts"].get(ref, {})
                preferred = 0 if ref == primary else 1 if ref == active else 2
                return preferred, float(state.get("last_used") or 0), ref

            return [AccountCandidate(ref, self._managers[ref]) for ref in sorted(healthy, key=rank)]

    def mark_success(self, ref: str) -> None:
        with self._lock:
            if ref not in self._managers:
                return
            state = self._state["accounts"].setdefault(ref, {})
            state.update({
                "cooldown_until": 0,
                "reason": "",
                "failures": 0,
                "last_used": time.time(),
            })
            self._state["active_ref"] = ref
            self._save_locked()

    def mark_failure(self, ref: str, reason: str, cooldown_seconds: int) -> None:
        with self._lock:
            if ref not in self._managers:
                return
            state = self._state["accounts"].setdefault(ref, {})
            state["reason"] = str(reason)[:160]
            state["failures"] = int(state.get("failures") or 0) + 1
            state["cooldown_until"] = time.time() + max(1, int(cooldown_seconds))
            if self._state.get("active_ref") == ref:
                self._state["active_ref"] = None
            self._save_locked()

    # ---------------- points ----------------

    def points_stale(self) -> bool:
        now = time.time()
        with self._lock:
            accounts = self._state["accounts"]
            if not accounts:
                return False
            for state in accounts.values():
                if now - float(state.get("points_ts") or 0) > POINTS_TTL:
                    return True
            return False

    async def refresh_points(self, force: bool = False) -> dict:
        """Re-read every account balance, at most one flight at a time."""
        async with self._points_lock:
            with self._lock:
                managers = list(self._managers.items())
                snapshot = json.loads(json.dumps(self._state))
            if not managers:
                return self.summary()
            now = time.time()
            updates: dict[str, dict] = {}
            for ref, manager in managers:
                state = snapshot["accounts"].get(ref, {})
                if not force and now - float(state.get("points_ts") or 0) <= POINTS_TTL:
                    updates[ref] = {"points": state.get("points"), "points_ts": state.get("points_ts")}
                    continue
                data = await manager.points()
                if data is None:
                    updates[ref] = {"points": None, "points_ts": time.time(),
                                    "points_error": "balance read failed"}
                    continue
                updates[ref] = {
                    "points": data.get("available_points"),
                    "points_ts": time.time(),
                    "daily_points": data.get("daily_points"),
                    "reward_points": data.get("reward_points"),
                }
            with self._lock:
                for ref, patch in updates.items():
                    self._state["accounts"].setdefault(ref, {}).update(patch)
                self._save_locked()
            return self.summary()

    # ---------------- views ----------------

    def status(self) -> list[dict]:
        now = time.time()
        with self._lock:
            entries = list(self._managers.items())
            state_snapshot = json.loads(json.dumps(self._state))
        result: list[dict] = []
        for ref, manager in entries:
            state = state_snapshot["accounts"].get(ref, {})
            cooldown_until = float(state.get("cooldown_until") or 0)
            try:
                summary = manager.summary()
                token_expired = bool(summary.get("token_expired"))
                item_state = "cooling" if cooldown_until > now else "expired" if token_expired else "ready"
                result.append({
                    "ref": ref,
                    "name": summary.get("name") or "xhx account",
                    "uid": _mask_uid(summary.get("uid")),
                    "nation_code": summary.get("nation_code") or "",
                    "state": item_state,
                    "primary": ref == state_snapshot.get("primary_ref"),
                    "active": ref == state_snapshot.get("active_ref"),
                    "reason": state.get("reason") or "",
                    "points": state.get("points"),
                    "daily_points": state.get("daily_points"),
                    "reward_points": state.get("reward_points"),
                    "points_error": state.get("points_error") or "",
                    "points_updated_at": _iso_timestamp(state.get("points_ts")),
                    "cooldown_until": _iso_timestamp(cooldown_until),
                    "last_used": _iso_timestamp(state.get("last_used")),
                    "token_expires_at": _iso_timestamp(float(summary.get("token_expires_at") or 0) / 1000),
                })
            except Exception:
                result.append({
                    "ref": ref,
                    "name": "xhx account",
                    "uid": "****",
                    "nation_code": "",
                    "state": "error",
                    "primary": ref == state_snapshot.get("primary_ref"),
                    "active": ref == state_snapshot.get("active_ref"),
                    "reason": "credential unreadable",
                    "points": None,
                    "points_error": "",
                    "points_updated_at": None,
                    "cooldown_until": _iso_timestamp(cooldown_until),
                    "last_used": _iso_timestamp(state.get("last_used")),
                    "token_expires_at": None,
                })
        result.sort(key=lambda item: (not item["primary"], not item["active"], item["name"], item["ref"]))
        return result

    def summary(self) -> dict:
        accounts = self.status()
        return {
            "accounts": accounts,
            "count": len(accounts),
            "ready": sum(item["state"] == "ready" for item in accounts),
            "cooling": sum(item["state"] == "cooling" for item in accounts),
            "auth_dir": str(self.auth_dir),
        }

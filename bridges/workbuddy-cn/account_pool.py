"""Local, credential-safe WorkBuddy account pool.

The pool owns copies of WorkBuddy session files under the bridge's ``auths``
directory. It never writes to the official WorkBuddy login directory.
"""

from __future__ import annotations

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
ACCOUNT_GLOB = "workbuddy-*.json"


def _account_ref(uid: str) -> str:
    return hashlib.sha256(uid.encode("utf-8")).hexdigest()[:16]


def _mask_uid(uid: object) -> str:
    text = str(uid or "").strip()
    if len(text) <= 4:
        return "••••"
    if len(text) <= 8:
        return f"{text[:2]}••{text[-2:]}"
    return f"{text[:4]}••••{text[-4:]}"


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
        identity = subprocess.check_output(
            ["whoami"], text=True, encoding="utf-8", errors="replace",
        ).strip()
        if not identity:
            raise OSError("无法确定当前 Windows 用户")
        own_rule = f"{identity}:(OI)(CI)F" if path.is_dir() else f"{identity}:(F)"
        system_rule = "*S-1-5-18:(OI)(CI)F" if path.is_dir() else "*S-1-5-18:(F)"
        result = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", own_rule, system_rule],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=15, check=False,
        )
        if result.returncode != 0:
            raise OSError(result.stderr.strip() or result.stdout.strip() or "icacls failed")
    except (OSError, subprocess.SubprocessError) as exc:
        raise OSError(f"无法保护本地凭据权限：{path}") from exc


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


class AccountPool:
    """Persisted preferred-account pool with bounded cooldown state."""

    def __init__(
        self,
        auth_dir: Path,
        manager_factory: Callable[[Path], Any],
        official_finder: Callable[[], Path | None],
        *,
        auto_import: bool = True,
        forbidden_dirs: list[Path] | None = None,
    ) -> None:
        self.auth_dir = auth_dir.resolve()
        for forbidden in forbidden_dirs or []:
            resolved = forbidden.resolve()
            if self.auth_dir == resolved or resolved in self.auth_dir.parents:
                raise ValueError("Bridge auths 目录不能是官方 WorkBuddy 登录目录或其子目录")
        self.state_path = self.auth_dir / STATE_FILE_NAME
        self._manager_factory = manager_factory
        self._official_finder = official_finder
        self._lock = threading.RLock()
        self._managers: dict[str, Any] = {}
        self._paths: dict[str, Path] = {}
        self._state: dict = {"primary_ref": None, "active_ref": None, "accounts": {}}
        self.auth_dir.mkdir(parents=True, exist_ok=True)
        harden_private_path(self.auth_dir)
        self._load_state()
        self.reload()
        if auto_import and not self._managers:
            try:
                self.import_current()
            except (OSError, ValueError, RuntimeError):
                pass

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

    @staticmethod
    def _session_identity(session: dict) -> tuple[str, str, str]:
        account = session.get("account") if isinstance(session, dict) else None
        auth = session.get("auth") if isinstance(session, dict) else None
        if not isinstance(account, dict) or not isinstance(auth, dict):
            raise ValueError("WorkBuddy 凭据缺少 account/auth")
        uid = str(account.get("uid") or "").strip()
        if not uid or not auth.get("accessToken"):
            raise ValueError("WorkBuddy 凭据缺少 uid/accessToken")
        name = str(account.get("nickname") or account.get("name") or "WorkBuddy account").strip()
        enterprise = str(account.get("enterpriseName") or "").strip()
        return uid, name, enterprise

    def reload(self) -> None:
        with self._lock:
            managers: dict[str, Any] = {}
            paths: dict[str, Path] = {}
            for path in sorted(self.auth_dir.glob(ACCOUNT_GLOB)):
                try:
                    session = json.loads(path.read_text(encoding="utf-8"))
                    uid, _, _ = self._session_identity(session)
                    ref = _account_ref(uid)
                    managers[ref] = self._manager_factory(path)
                    paths[ref] = path
                    self._state["accounts"].setdefault(ref, {})
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
        source = self._official_finder()
        if source is None or not source.is_file():
            raise RuntimeError("未找到 WorkBuddy 当前登录凭据，请先在官方客户端登录")
        try:
            session = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("无法读取 WorkBuddy 当前登录凭据") from exc
        return self.add_session(session)

    def add_session(self, session: dict) -> dict:
        """Store one authenticated WorkBuddy session inside the isolated pool."""
        uid, _, _ = self._session_identity(session)
        ref = _account_ref(uid)
        destination = self.auth_dir / f"workbuddy-{ref}.json"
        _atomic_json(destination, session)
        self.reload()
        with self._lock:
            state = self._state["accounts"].setdefault(ref, {})
            state.update({"cooldown_until": 0, "reason": "", "failures": 0})
            if not self._state.get("primary_ref"):
                self._state["primary_ref"] = ref
            self._save_locked()
        return self.get_account(ref)

    def manager_for(self, ref: str) -> Any:
        """Return the credential manager for an opaque account reference."""
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
                raise RuntimeError("账号文件不在本地 auths 目录")
            resolved.unlink(missing_ok=True)
            self._state["accounts"].pop(ref, None)
            if self._state.get("primary_ref") == ref:
                self._state["primary_ref"] = None
            if self._state.get("active_ref") == ref:
                self._state["active_ref"] = None
            self.reload()

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
                    "name": summary.get("nickname") or "WorkBuddy account",
                    "uid": _mask_uid(summary.get("uid")),
                    "enterprise_name": summary.get("enterpriseName") or "",
                    "state": item_state,
                    "primary": ref == state_snapshot.get("primary_ref"),
                    "active": ref == state_snapshot.get("active_ref"),
                    "reason": state.get("reason") or "",
                    "cooldown_until": _iso_timestamp(cooldown_until),
                    "last_used": _iso_timestamp(state.get("last_used")),
                    "token_expires_at": _iso_timestamp(
                        float(summary.get("token_expires_at") or 0) / 1000
                    ),
                })
            except Exception:
                result.append({
                    "ref": ref,
                    "name": "WorkBuddy account",
                    "uid": "••••",
                    "enterprise_name": "",
                    "state": "error",
                    "primary": ref == state_snapshot.get("primary_ref"),
                    "active": ref == state_snapshot.get("active_ref"),
                    "reason": "凭据无法读取",
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

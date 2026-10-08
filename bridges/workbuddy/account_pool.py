"""Local, credential-safe WorkBuddy account pool.

The pool owns copies of WorkBuddy session files under the bridge's ``auths``
directory. It never writes to the official WorkBuddy login directory.

The pool skeleton -- the state file, the lock, the cooldown bookkeeping, the
primary/active/LRU order, mark_success/mark_failure and the summary rows --
lives in ``_account_pool.py``, shared with xhx, plan_key_pool and gemini.
What stays here is what only WorkBuddy has: the session-file shape, the
forbidden official-login directories, and the uid masking the panel shows.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Callable

# bridges/ 自己也带公共模块（_common），和上面几个共享模块同一个套路。
_BRIDGES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir)
if _BRIDGES_DIR not in sys.path:
    sys.path.insert(0, _BRIDGES_DIR)

import _account_pool
from _account_pool import STATE_FILE_NAME, account_ref, atomic_json, iso_timestamp

ACCOUNT_GLOB = "workbuddy-*.json"

#: uid -> the pool's stable 16-hex ref (the file name and the state key)
_account_ref = account_ref


def _mask_uid(uid: object) -> str:
    text = str(uid or "").strip()
    if len(text) <= 4:
        return "••••"
    if len(text) <= 8:
        return f"{text[:2]}••{text[-2:]}"
    return f"{text[:4]}••••{text[-4:]}"


class AccountCandidate(_account_pool.AccountCandidate):
    """One pickable WorkBuddy session, with the manager a request goes through."""

    def __init__(self, ref: str, manager: Any) -> None:
        super().__init__(ref)
        self.manager = manager


class AccountPool(_account_pool.AccountPool):
    """Persisted preferred-account pool with bounded cooldown state."""

    def __init__(
        self,
        auth_dir,
        manager_factory: Callable[[Any], Any],
        official_finder: Callable[[], Any],
        *,
        auto_import: bool = True,
        forbidden_dirs: list | None = None,
    ) -> None:
        self.auth_dir = os.path.realpath(str(auth_dir))
        for forbidden in forbidden_dirs or []:
            resolved = os.path.realpath(str(forbidden))
            if self.auth_dir == resolved or resolved in self.auth_dir.split(os.sep):
                raise ValueError("Bridge auths 目录不能是官方 WorkBuddy 登录目录或其子目录")
        self._manager_factory = manager_factory
        self._official_finder = official_finder
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
                session = json.loads(path.read_text(encoding="utf-8"))
                uid, _, _ = self._session_identity(session)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
            out.append({"ref": _account_ref(uid), "path": path,
                        "name": _mask_uid(uid)})
        return out

    @property
    def _managers(self) -> dict:
        """ref -> the manager a request is served through (the session file)."""
        return {ref: row["path"] for ref, row in self._rows.items()}

    def account_path(self, ref: str):
        """The session file this account's credential lives in."""
        row = self._rows.get(ref)
        return row["path"] if row else None

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

    # ---------------- import ----------------

    def import_current(self) -> dict:
        source = self._official_finder()
        if source is None or not os.path.isfile(str(source)):
            raise RuntimeError("未找到 WorkBuddy 当前登录凭据，请先在官方客户端登录")
        try:
            session = json.loads(open(str(source), encoding="utf-8").read())
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("无法读取 WorkBuddy 当前登录凭据") from exc
        return self.add_session(session)

    def add_session(self, session: dict) -> dict:
        """Store one authenticated WorkBuddy session inside the isolated pool."""
        uid, _, _ = self._session_identity(session)
        ref = _account_ref(uid)
        destination = self.auth_dir / f"workbuddy-{ref}.json"
        atomic_json(destination, session)
        self.reload()
        with self._lock:
            state = self._state_of(ref)
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

    # ---------------- selection ----------------

    def candidates(self) -> list[AccountCandidate]:
        return [AccountCandidate(ref, self._managers[ref])
                for ref in self._ready_refs()]

    # ---------------- views ----------------

    def account_row(self, row: dict, state: dict) -> dict:
        """The base row plus the uid and enterprise the panel shows."""
        entry = super().account_row(row, state)
        entry.update({"uid": row.get("name") or "",
                      "enterprise": state.get("enterprise") or ""})
        return entry

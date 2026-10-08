"""Shared account-pool scaffolding for the credential-backed FleetKit bridges.

Five bridges (workbuddy, xhx, plan_key_pool for kimi-code/minimax, and
gemini) started as copies of the first one, so the same persisted pool
skeleton were copied and had already drifted apart: one keeps its UTC
timestamps localised and another does not, one masks a reason at 160
characters and another at 300, one shields the credential directory with
icacls and another only chmods. Same contract, five spellings.

This module keeps the skeleton in one place: the state file, the lock, the
cooldown bookkeeping, the mark_success/mark_failure pair, the
primary/active/LRU ordering, and the summary rows. Each pool still owns
everything provider-specific (how a credential file is scanned, what an
account summary contains, whether a token expiry matters); it just stops
re-implementing the parts that are identical.

Import it the way the bridges already import _common:

    import os, sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))
    import _account_pool as poolmod

Helpers live here only when they are behaviour-preserving across providers;
anything a bridge needs to do differently stays in that bridge.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

STATE_FILE_NAME = "pool-state.json"

# How often one account balance is re-read for the panel (seconds). The
# balance endpoint is a read-only GET, but there is no reason to hit every
# account on each /health poll.
POINTS_TTL = float(os.environ.get("FLEET_POINTS_TTL") or "300")

#: the per-account quota keys refresh_points() writes and status() reads
POINTS_ROW_FIELDS = ("points", "points_unit", "points_plan", "points_detail",
                     "points_error", "daily_points", "reward_points")

#: what a cached (still fresh) points entry keeps when refresh_points() runs
POINTS_TTL_FIELDS = POINTS_ROW_FIELDS + ("points_ts",)


def account_ref(key: str) -> str:
    """Stable 16-hex id for one credential; the file name and the state key."""
    return hashlib.sha256(str(key or "").encode("utf-8")).hexdigest()[:16]


def account_name(brand: str, key: str) -> str:
    """Never log a key whole: "kimi...1a09" tells two keys apart."""
    text = str(key or "").strip()
    tail = text[-4:] if len(text) >= 8 else "****"
    return "%s\u2026%s" % (brand, tail)


def iso_timestamp(value: Optional[float]) -> Optional[str]:
    """Epoch seconds -> local ISO-8601 with second precision, or None."""
    if not value:
        return None
    try:
        return datetime.fromtimestamp(float(value), timezone.utc).astimezone().isoformat(
            timespec="seconds")
    except (OSError, OverflowError, TypeError, ValueError):
        return None


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


def harden_private_path(path: Path) -> None:
    """Restrict a credential directory/file to the current user and SYSTEM."""
    if os.name != "nt":
        if path.is_dir():
            os.chmod(path, 0o700)
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


def atomic_json(path: Path, payload: dict) -> None:
    """Write JSON through a temp file, leaving a 0600 file behind."""
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


class PoolExhausted(RuntimeError):
    """No account was reachable; the bridge answers the pool's own 503."""


class AccountCandidate:
    """One pickable account. Subclasses add whatever a bridge needs per call."""

    def __init__(self, ref: str) -> None:
        self.ref = ref

    @property
    def key(self) -> str:
        """The secret this candidate authenticates with, for logging masks."""
        raise NotImplementedError

    def __repr__(self) -> str:
        return "%s(%r)" % (type(self).__name__, self.ref)


class AccountPool:
    """Persisted preferred-account pool with bounded cooldown state.

    A pool owns a directory of credential files plus one pool-state.json
    beside them. Scanning is a hook (scan()) because every provider reads a
    different shape: workbuddy/xhx a JSON session, plan_key_pool a bare key
    file, gemini a directory per account. Everything else -- the state file,
    the lock, the cooldown bookkeeping, the primary/active/LRU order, the
    mark_success/mark_failure pair, the summary rows -- is the same, so it
    lives here.

    Subclass contract:

    * scan() returns a list of rows, each with at least {"ref": ...}.
    * candidate(row) turns one scanned row into a candidate object.
    * account_row(row, state) turns one scanned row plus its state into the
      status dict the panel and /health read.
    """

    def __init__(self, auth_dir: Path, *, state_file: Optional[str] = None) -> None:
        self.auth_dir = Path(auth_dir).resolve()
        self.state_path = self.auth_dir / (state_file or STATE_FILE_NAME)
        self._lock = threading.RLock()
        self._points_lock: Optional[asyncio.Lock] = None
        # left unset: a subclass that takes read_points= as an argument has
        # already assigned it before calling super(), and None must not wipe it
        if not hasattr(self, "_points_reader"):
            self._points_reader = None
        self._state: dict = {"primary_ref": None, "active_ref": None, "accounts": {}}
        self.points_ttl = POINTS_TTL
        self.auth_dir.mkdir(parents=True, exist_ok=True)
        harden_private_path(self.auth_dir)
        self._load_state()
        self.reload()

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
        except (OSError, ValueError, TypeError):
            return

    def _save_locked(self) -> None:
        atomic_json(self.state_path, self._state)

    # ---------------- scanning ----------------

    def scan(self) -> list:
        """Every credential this pool can use, oldest-stable first.

        Returned rows need at least {"ref": str}; subclass hooks decide the
        rest. Must be safe to call on every reload: it reads the directory
        only, and must not mutate the pool.
        """
        raise NotImplementedError

    def reload(self) -> None:
        """Re-scan the credential directory, keeping whatever survived.

        A disappeared account drops out of the state, and a primary that
        vanished repoints to the first account scan() returned -- the order
        four copies each made slightly differently.
        """
        with self._lock:
            rows = self.scan()
            self._rows = {row["ref"]: row for row in rows}
            valid = set(self._rows)
            accounts = {
                ref: value for ref, value in self._state["accounts"].items()
                if ref in valid and isinstance(value, dict)
            }
            # A newly scanned account starts with no state at all; without the
            # setdefault the first mark_failure() is what creates it, and
            # points_stale() reads a missing key in the meantime.
            for ref in self._rows:
                accounts.setdefault(ref, {})
            self._state["accounts"] = accounts
            if self._state.get("primary_ref") not in valid:
                self._state["primary_ref"] = rows[0]["ref"] if rows else None
            if self._state.get("active_ref") not in valid:
                self._state["active_ref"] = None
            self._save_locked()

    def _state_of(self, ref: str) -> dict:
        return self._state["accounts"].setdefault(ref, {})

    # ---------------- selection ----------------

    def _ready_refs(self, ignore_cooldown: bool = False) -> list:
        """Pickable refs: primary, then active, then least recently used.

        The tie-break is position in scan() order, not the ref string: a pool
        with three keys added in one second all share last_used=0, and the
        order they were scanned in is the only stable, human-predictable one
        (planet_key_pool sorted its file names).
        """
        now = time.time()
        with self._lock:
            primary = self._state.get("primary_ref")
            active = self._state.get("active_ref")
            order = {ref: index for index, ref in enumerate(self._rows)}

            def rank(ref: str) -> tuple:
                state = self._state["accounts"].get(ref, {})
                preferred = 0 if ref == primary else 1 if ref == active else 2
                return (preferred, -float(state.get("last_used") or 0), order[ref])

            return sorted(
                (ref for ref in self._rows
                 if ignore_cooldown
                 or float(self._state["accounts"].get(ref, {}).get("cooldown_until") or 0) <= now),
                key=rank,
            )

    def candidates(self, ignore_cooldown: bool = False) -> list:
        """Pickable accounts, primary first, then active, then least used."""
        return [self.candidate(self._rows[ref])
                for ref in self._ready_refs(ignore_cooldown)]

    def candidate(self, row: dict) -> Any:
        """The object one request is served through, for one scanned row.

        A plain AccountCandidate by default; a bridge whose request path
        needs more than the ref (plan_key_pool hands it the key) overrides
        this.
        """
        return AccountCandidate(row["ref"])

    def candidate_factory(self, row: dict) -> Any:
        return self.candidate(row)

    # ---------------- result writeback ----------------

    def mark_success(self, ref: str) -> None:
        with self._lock:
            if ref not in self._rows:
                return
            state = self._state_of(ref)
            state.update({"cooldown_until": 0, "reason": "", "failures": 0,
                          "last_used": time.time()})
            self._state["active_ref"] = ref
            self._save_locked()

    def mark_failure(self, ref: str, reason: str, cooldown_seconds: float) -> None:
        with self._lock:
            if ref not in self._rows:
                return
            state = self._state_of(ref)
            state["reason"] = str(reason)[:160]
            state["failures"] = int(state.get("failures") or 0) + 1
            state["cooldown_until"] = time.time() + max(1, cooldown_seconds)
            if self._state.get("active_ref") == ref:
                self._state["active_ref"] = None
            self._save_locked()

    def account_path(self, ref: str):
        """Where this account's credential lives on disk, or None.

        Overridden by a pool that stores one file per account; a pool that
        keeps its credentials elsewhere has nothing to remove.
        """
        return None

    def remove(self, ref: str) -> None:
        """Delete one credential file, refusing anything the pool does not own.

        The check is on the path the *pool* recorded, not on a handle a caller
        may have swapped out from under it: a credential whose file was moved
        somewhere else is left where it is, because the pool's auths/ is the
        only place it is allowed to delete from.
        """
        with self._lock:
            path = self.account_path(ref)
            if path is None:
                raise KeyError(ref)
            resolved = path.resolve()
            if resolved.parent != self.auth_dir:
                raise RuntimeError("account file is not inside the local auths dir")
            resolved.unlink(missing_ok=True)
            self.reload()

    def set_primary(self, ref: str) -> None:
        with self._lock:
            if ref not in self._rows:
                raise KeyError(ref)
            self._state["primary_ref"] = ref
            self._save_locked()

    # ---------------- views ----------------

    def account_row(self, row: dict, state: dict) -> dict:
        """One status row. Subclasses add provider-specific fields."""
        now = time.time()
        cooldown_until = float(state.get("cooldown_until") or 0)
        entry = {
            "ref": row["ref"],
            "name": row.get("name") or row["ref"],
            "state": "cooling" if cooldown_until > now else "ready",
            "primary": row["ref"] == self._state.get("primary_ref"),
            "active": row["ref"] == self._state.get("active_ref"),
            "reason": state.get("reason") or "",
            "failures": int(state.get("failures") or 0),
            "cooldown_until": iso_timestamp(cooldown_until),
            "last_used": iso_timestamp(state.get("last_used")),
        }
        # Points are a panel concern only, but every pool that has them wants
        # the same five keys -- so they ride along instead of being re-typed
        # in each subclass.
        entry.update({key: state.get(key) for key in POINTS_ROW_FIELDS})
        entry["points_updated_at"] = iso_timestamp(state.get("points_ts"))
        return entry

    def status(self) -> list:
        with self._lock:
            order = {ref: index for index, ref in enumerate(self._rows)}
            rows = [self.account_row(row, self._state["accounts"].get(ref, {}))
                    for ref, row in self._rows.items()]
        rows.sort(key=lambda item: (not item["primary"], not item["active"],
                                    order[item["ref"]], item["ref"]))
        return rows

    def get_account(self, ref: str) -> dict:
        for item in self.status():
            if item["ref"] == ref:
                return item
        raise KeyError(ref)

    def summary(self) -> dict:
        accounts = self.status()
        return {
            "accounts": accounts,
            "count": len(accounts),
            "ready": sum(item["state"] == "ready" for item in accounts),
            "cooling": sum(item["state"] == "cooling" for item in accounts),
            "auth_dir": str(self.auth_dir),
        }

    # ---------------- points ----------------

    def points_stale(self) -> bool:
        now = time.time()
        with self._lock:
            accounts = self._state["accounts"]
            if not accounts:
                return False
            for state in accounts.values():
                if now - float(state.get("points_ts") or 0) > self.points_ttl:
                    return True
            return False

    def _points_lock_holder(self) -> asyncio.Lock:
        # asyncio.Lock() must be built inside a running loop policy, so it is
        # created on first use rather than in __init__ (a pool is built while
        # the bridge's event loop may not exist yet).
        if self._points_lock is None:
            self._points_lock = asyncio.Lock()
        return self._points_lock

    async def refresh_points(self, force: bool = False) -> dict:
        """Re-read every account quota, at most one flight at a time.

        The reader is provider-specific, so it is injected as read_points(row)
        -- a plain callable or a coroutine, whichever the provider needs -- and
        a blocking one runs on a worker thread: a quota read must never stall
        the event loop that is also serving chat. A pool without a reader just
        returns the summary.
        """
        if self._points_reader is None and type(self).read_points is AccountPool.read_points:
            # no reader injected and no subclass reader: nothing to re-read
            return self.summary()
        async with self._points_lock_holder():
            with self._lock:
                rows = dict(self._rows)
                snapshot = json.loads(json.dumps(self._state))
            if not rows:
                return self.summary()
            now = time.time()
            updates: dict = {}
            for ref, row in rows.items():
                state = snapshot["accounts"].get(ref, {})
                if not force and now - float(state.get("points_ts") or 0) <= self.points_ttl:
                    updates[ref] = {key: state.get(key) for key in POINTS_TTL_FIELDS}
                    continue
                try:
                    data = self.read_points(row)
                    if inspect.isawaitable(data):
                        data = await data
                except Exception as exc:
                    updates[ref] = {"points": None, "points_ts": time.time(),
                                    "points_error": "%s: %s" % (type(exc).__name__, str(exc)[:120])}
                    continue
                updates[ref] = self.points_row(data if isinstance(data, dict) else {})
            with self._lock:
                for ref, patch in updates.items():
                    self._state["accounts"].setdefault(ref, {}).update(patch)
                self._save_locked()
            return self.summary()

    def set_points_reader(self, reader) -> None:
        """Swap in (or clear) the quota reader.

        A pool with no reader just serves whatever summary() already holds,
        which is what the plan bridges do before their first quota call.
        """
        self._points_reader = reader

    def read_points(self, row: dict):
        """One row's quota, through whatever reader the bridge injected.

        The reader receives the whole row so a subclass can hand its provider
        the exact shape it wants (plan_key_pool passes the bare key); a pool
        with no reader answers None and refresh_points() keeps the last read.
        """
        if self._points_reader is None:
            return None
        return self._points_reader(row)

    def points_row(self, data: dict) -> dict:
        """What a reader's reply becomes in the state, plus its timestamp.

        Overridden where a provider reports more than the generic four keys:
        xhx splits one balance into daily and reward points.
        """
        row = {
            "points": data.get("points"),
            "points_unit": str(data.get("unit") or ""),
            "points_plan": str(data.get("plan") or ""),
            "points_detail": str(data.get("detail") or ""),
            "points_error": str(data.get("error") or ""),
        }
        row["points_ts"] = time.time()
        return row

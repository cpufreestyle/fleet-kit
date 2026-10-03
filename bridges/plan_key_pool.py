"""Generic per-key account pool for the plan/credit bridges (Kimi Code, MiniMax).

Two bridges bill against a *subscription* rather than a free tier, so the
question "which key is this request burning" is the one the operator has to
answer by hand: when the only key hits a rate limit the whole node dies until
somebody pastes a second key into fleet.env and restarts the service.

xhx already solved the shape of this problem in bridges/xhx/account_pool.py,
and workbuddy solved it before that. What differs here is only the *unit* of
an account: xhx holds a session file with a single-use refresh_token, these
two hold a plain API key. So the session machinery (JWT identity, refresh
rotation, token expiry) drops out, and what is left is:

* one key per account file under the bridge's auths/ directory, 0600
* candidates() -> primary, then active, then least-recently-used, all
  accounts in cooldown skipped
* mark_success/mark_failure give bounded cooldown per key
* status()/summary() are what /health and the panel render
* refresh_points() re-reads each key's quota through an injected reader,
  because Kimi publishes /v1/usages and MiniMax publishes
  /v1/token_plan/remains and neither bridge should know the other's URL

Everything provider-specific -- which env vars seed the first key, what a 403
means for that provider, where the quota endpoint is -- stays in the bridge,
which passes it in at construction time. That is why this module exists
instead of a third copy of the pool.

Seeding is one-way on purpose, the same rule xhx follows: the pool only ever
writes files under its own auths/ directory. The Kimi desktop app's key file
is read, never written, and KIMI_NO_APP_KEY turns even the read off.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading
import time
from typing import Any, Callable, Optional


STATE_FILE_NAME = "pool-state.json"

# How often one account's quota is re-read for the panel (seconds). Every
# reader is a vendor GET, so the panel drifts by at most this much rather than
# hitting every account on each /health poll.
POINTS_TTL = float(os.environ.get("PLAN_POOL_POINTS_TTL") or "300")

# A failure verdict is (reason, cooldown_seconds, tag). The tag is what lets
# the bridge phrase the final error: "plan_inactive" on Kimi must say renew,
# not "your key is dead", because /v1/models answers 200 with that same key.


def account_ref(key: str) -> str:
    """Stable 16-hex id for one key; the file name and the pool state key."""
    return hashlib.sha256(str(key or "").encode("utf-8")).hexdigest()[:16]


def account_name(brand: str, key: str) -> str:
    """Never log a key whole: "kimi...1a09" tells two keys apart."""
    text = str(key or "").strip()
    tail = text[-4:] if len(text) >= 8 else "****"
    return "%s\u2026%s" % (brand, tail)


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


def _read_key_file(path: Path) -> str:
    """The key one account file carries, or "" when it carries none."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return ""
    return str(data.get("key") or "").strip()


def keys_from_env(env_name: str, keys_env: str = "") -> list:
    """Every key the environment offers, as [(key, where)] pairs.

    <KEYS name> is comma/space separated so one fleet.env line can seed a
    whole pool; <NAME> stays the single-key spelling the bridges already
    document, and an operator who only ever sets that one is not penalised.
    The plural defaults to <NAME>_KEYS, which is not a derivation every brand
    shares -- MINIMAX_API_KEY pluralises to MINIMAX_API_KEYS, not
    MINIMAX_API_KEY_KEYS -- so a caller passes the spelling its docs use and
    each pair names the variable the key actually came from.
    """
    keys_env = keys_env or env_name + "_KEYS"
    pairs, seen = [], set()
    for origin in (keys_env, env_name):
        for key in (os.environ.get(origin) or "").replace(",", " ").split():
            if key and key not in seen:
                seen.add(key)
                pairs.append((key, "env " + origin))
    return pairs


def walk_numbers(body: Any, limit: int = 8) -> list:
    """Every number in a quota payload, as (path, value) pairs.

    The payload shape is only knowable once a live key answers, and Kimi's
    usage windows and MiniMax's token-plan remains do not share a schema, so
    the readers report what is actually there instead of hard-coding keys
    that may not exist in the reply. Booleans are skipped: "true" is not a
    balance.
    """
    found: list = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, "%s/%s" % (path, key))
        elif isinstance(node, list):
            for value in node:
                walk(value, path + "[]")
        elif isinstance(node, bool):
            return
        elif isinstance(node, (int, float)):
            found.append((path.lstrip("/"), node))

    walk(body, "")
    return found[:limit]


def headline_number(numbers: list) -> tuple:
    """(value, unit) for the number that answers "how much is left".

    A remaining-shaped path wins over any other number: a payload that
    carries both a used counter and a remaining counter must not show the
    used one. Falls back to the first number so a payload with an unknown
    shape still renders something rather than nothing.
    """
    for path, value in numbers:
        if any(word in path.lower()
               for word in ("remain", "left", "quota", "balance")):
            return value, path.rsplit("/", 1)[-1]
    if numbers:
        return numbers[0][1], numbers[0][0].rsplit("/", 1)[-1]
    return None, ""


@dataclass(frozen=True)
class KeyAccount:
    """One pooled key: where it lives and what it is, nothing else."""
    ref: str
    key: str
    path: Path


@dataclass(frozen=True)
class AccountCandidate:
    ref: str
    account: KeyAccount


class PoolUnavailable(RuntimeError):
    """No candidate could be tried at all: the pool is empty or all cooling."""


class PoolExhausted(RuntimeError):
    """Every candidate was tried and every one failed at the account level.

    failures is a list of (ref, status, body, reason, tag) in the order they
    were tried, so the bridge can phrase the reply from the provider's own
    vocabulary instead of a generic "all keys failed".
    """

    def __init__(self, failures: list) -> None:
        self.failures = list(failures)
        last = self.failures[-1] if self.failures else ("?", 502, "", "no candidate", "unknown")
        self.status = last[1]
        self.body = last[2]
        self.reason = last[3]
        super().__init__("every pooled account failed (%s)" % self.reason)


async def request_with_pool(pool: "KeyPool", send: Callable, classify: Callable):
    """Run send(key) over the pool's candidates until one answers.

    send is an async callable taking one key and returning an httpx.Response
    that has already been sent; the bridge owns the URL, the body and the
    headers shape, this loop owns only which key is used and what happens
    when a key stops working.

    Returns (candidate, response). A 200 marks the account healthy and comes
    back immediately. A non-200 goes through classify first: a verdict cools
    the account and moves to the next candidate, no verdict means the reply
    is the provider's answer to the *request* (a 400, a 404 model) and is
    returned untouched for the caller to pass through.

    Raises PoolUnavailable when there was nothing to try and PoolExhausted
    when every candidate failed at the account level.
    """
    candidates = pool.candidates()
    if not candidates:
        raise PoolUnavailable("account pool has no ready account")
    failures: list = []
    for candidate in candidates:
        try:
            response = await send(candidate.account.key)
        except Exception as exc:
            # A transport error says nothing about the key, but it does say
            # this account cannot serve right now, so it is cooled briefly.
            pool.mark_failure(candidate.ref, "transport %s: %s" % (type(exc).__name__, str(exc)[:80]), 30)
            failures.append((candidate.ref, 0, str(exc)[:400], "transport error", "transport"))
            continue
        status = getattr(response, "status_code", 502)
        if status == 200:
            pool.mark_success(candidate.ref)
            return candidate, response
        body = (await response.aread()).decode("utf-8", "replace")
        verdict = classify(status, body)
        if verdict is None:
            return candidate, response
        reason, cooldown, tag = verdict
        pool.mark_failure(candidate.ref, reason, cooldown)
        failures.append((candidate.ref, status, body[:400], reason, tag))
        await response.aclose()
    raise PoolExhausted(failures)


class KeyPool:
    """Persisted preferred-key pool with bounded cooldown state."""

    def __init__(
        self,
        brand: str,
        auth_dir: Path,
        seed: Callable[[], list],
        *,
        read_points: Optional[Callable[[str], dict]] = None,
        points_ttl: Optional[float] = None,
    ) -> None:
        self.brand = str(brand or "key")
        self.auth_dir = Path(auth_dir).resolve()
        self._seed = seed
        self._read_points = read_points
        self.points_ttl = float(points_ttl or POINTS_TTL)
        self._glob = self.brand + "-*.json"
        self.state_path = self.auth_dir / STATE_FILE_NAME
        self._lock = threading.RLock()
        self._accounts: dict = {}
        self._state: dict = {"primary_ref": None, "active_ref": None, "accounts": {}}
        self._points_lock = asyncio.Lock()
        self.auth_dir.mkdir(parents=True, exist_ok=True)
        harden_private_path(self.auth_dir)
        self._load_state()
        self.reload()
        if not self._accounts:
            self._seed_from_sources()

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
        _atomic_json(self.state_path, self._state)

    def reload(self) -> None:
        with self._lock:
            accounts: dict = {}
            for path in sorted(self.auth_dir.glob(self._glob)):
                try:
                    key = _read_key_file(path)
                except (OSError, ValueError, TypeError):
                    continue
                if not key:
                    continue
                ref = account_ref(key)
                accounts[ref] = KeyAccount(ref, key, path)
                self._state["accounts"].setdefault(ref, {})
            self._accounts = accounts
            valid = set(accounts)
            self._state["accounts"] = {
                ref: value for ref, value in self._state["accounts"].items()
                if ref in valid and isinstance(value, dict)
            }
            if self._state.get("primary_ref") not in valid:
                self._state["primary_ref"] = next(iter(accounts), None)
            if self._state.get("active_ref") not in valid:
                self._state["active_ref"] = None
            self._save_locked()

    def _seed_from_sources(self) -> None:
        """First run only: pull in every key the bridge says is available.

        A brand new machine has an empty auths/ directory but a working
        environment (or a desktop app key file), and a bridge that serves no
        models until somebody reads the docs first is a bridge that looks
        broken. Seeding is what makes "finish.sh kimi-code" enough.
        """
        try:
            offered = list(self._seed() or [])
        except Exception:
            offered = []
        for item in offered:
            # A seed is either a bare key or a (key, where) pair; Kimi names
            # the desktop app file a key came from and the panel shows it,
            # so the pair form is what the bridges hand over.
            if isinstance(item, (tuple, list)):
                key = str(item[0] or "").strip()
                source = str(item[1] or "").strip() if len(item) > 1 else ""
            else:
                key = str(item or "").strip()
                source = ""
            if not key:
                continue
            try:
                self.add(key, source=source)
            except (OSError, ValueError, RuntimeError):
                continue

    def add(self, key: str, source: str = "") -> dict:
        key = str(key or "").strip()
        if not key:
            raise ValueError("empty key")
        ref = account_ref(key)
        destination = self.auth_dir / (self.brand + "-" + ref + ".json")
        if destination.resolve().parent != self.auth_dir:
            raise RuntimeError("account file escapes the local auths dir")
        _atomic_json(destination, {"brand": self.brand, "key": key,
                                   "source": str(source or "")[:120],
                                   "added_at": _iso_timestamp(time.time())})
        self.reload()
        with self._lock:
            state = self._state["accounts"].setdefault(ref, {})
            state.update({"cooldown_until": 0, "reason": "", "failures": 0})
            state["source"] = str(source or "")[:120]
            if not self._state.get("primary_ref"):
                self._state["primary_ref"] = ref
            self._save_locked()
        return self.get_account(ref)

    def get_account(self, ref: str) -> dict:
        for item in self.status():
            if item["ref"] == ref:
                return item
        raise KeyError(ref)

    def set_primary(self, ref: str) -> None:
        with self._lock:
            if ref not in self._accounts:
                raise KeyError(ref)
            self._state["primary_ref"] = ref
            self._save_locked()

    def remove(self, ref: str) -> None:
        with self._lock:
            account = self._accounts.get(ref)
            if account is None:
                raise KeyError(ref)
            resolved = account.path.resolve()
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

    def candidates(self) -> list:
        """Ready accounts, primary first, then active, then least recently used."""
        now = time.time()
        with self._lock:
            healthy = [
                ref for ref in self._accounts
                if float(self._state["accounts"].get(ref, {}).get("cooldown_until") or 0) <= now
            ]
            primary = self._state.get("primary_ref")
            active = self._state.get("active_ref")

            def rank(ref: str) -> tuple:
                state = self._state["accounts"].get(ref, {})
                preferred = 0 if ref == primary else 1 if ref == active else 2
                return preferred, float(state.get("last_used") or 0), ref

            return [AccountCandidate(ref, self._accounts[ref])
                    for ref in sorted(healthy, key=rank)]

    def mark_success(self, ref: str) -> None:
        with self._lock:
            if ref not in self._accounts:
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
            if ref not in self._accounts:
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
                if now - float(state.get("points_ts") or 0) > self.points_ttl:
                    return True
            return False

    async def refresh_points(self, force: bool = False) -> dict:
        """Re-read every account quota, at most one flight at a time.

        The reader is blocking (urllib in plan_credits), so it runs on a
        worker thread: a quota read must never stall the event loop that is
        also serving chat.
        """
        if self._read_points is None:
            return self.summary()
        async with self._points_lock:
            with self._lock:
                entries = list(self._accounts.items())
                snapshot = json.loads(json.dumps(self._state))
            if not entries:
                return self.summary()
            now = time.time()
            updates: dict = {}
            for ref, account in entries:
                state = snapshot["accounts"].get(ref, {})
                if not force and now - float(state.get("points_ts") or 0) <= self.points_ttl:
                    updates[ref] = {key: state.get(key) for key in
                                    ("points", "points_unit", "points_plan",
                                     "points_detail", "points_error", "points_ts")}
                    continue
                try:
                    data = await asyncio.to_thread(self._read_points, account.key)
                except Exception as exc:
                    updates[ref] = {"points": None, "points_ts": time.time(),
                                    "points_error": "%s: %s" % (type(exc).__name__, str(exc)[:120])}
                    continue
                if not isinstance(data, dict):
                    data = {}
                updates[ref] = {
                    "points": data.get("points"),
                    "points_unit": str(data.get("unit") or ""),
                    "points_plan": str(data.get("plan") or ""),
                    "points_detail": str(data.get("detail") or ""),
                    "points_error": str(data.get("error") or ""),
                    "points_ts": time.time(),
                }
            with self._lock:
                for ref, patch in updates.items():
                    self._state["accounts"].setdefault(ref, {}).update(patch)
                self._save_locked()
            return self.summary()

    # ---------------- views ----------------

    def status(self) -> list:
        now = time.time()
        with self._lock:
            entries = list(self._accounts.items())
            state_snapshot = json.loads(json.dumps(self._state))
        result: list = []
        for ref, account in entries:
            state = state_snapshot["accounts"].get(ref, {})
            cooldown_until = float(state.get("cooldown_until") or 0)
            result.append({
                "ref": ref,
                "name": account_name(self.brand, account.key),
                "key_tail": str(account.key)[-4:],
                "source": state.get("source") or "",
                "state": "cooling" if cooldown_until > now else "ready",
                "primary": ref == state_snapshot.get("primary_ref"),
                "active": ref == state_snapshot.get("active_ref"),
                "reason": state.get("reason") or "",
                "points": state.get("points"),
                "points_unit": state.get("points_unit") or "",
                "points_plan": state.get("points_plan") or "",
                "points_detail": state.get("points_detail") or "",
                "points_error": state.get("points_error") or "",
                "points_updated_at": _iso_timestamp(state.get("points_ts")),
                "cooldown_until": _iso_timestamp(cooldown_until),
                "last_used": _iso_timestamp(state.get("last_used")),
            })
        result.sort(key=lambda item: (not item["primary"], not item["active"], item["ref"]))
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

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
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

# The pool skeleton (state file, cooldown bookkeeping, primary/active/LRU
# order, mark_success/failure, the points reader loop) lives in
# _account_pool.py, shared with workbuddy, xhx and gemini. What stays here is
# the part that is only true of a *key*: the account file shape, the seed
# sources, and the key-shaped status row.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _account_pool as poolmod
from _account_pool import (
    STATE_FILE_NAME,
    POINTS_TTL,
    account_name,
    iso_timestamp,
    harden_private_path,
    atomic_json,
)

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


class KeyPool(poolmod.AccountPool):
    """Persisted preferred-key pool with bounded cooldown state.

    The skeleton comes from _account_pool.AccountPool; a key is the unit here,
    so scan() is the key-file glob and the status row is key-shaped.
    """

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
        self._glob = self.brand + "-*.json"
        self._seed = seed
        self._points_reader = read_points
        self.points_ttl = float(points_ttl or POINTS_TTL)
        super().__init__(auth_dir)
        if not self._rows:
            self._seed_from_sources()

    # ---------------- scanning ----------------

    def scan(self) -> list:
        """Every key file in auths/, as [{ref, key, path, name, ...}]."""
        out = []
        for path in sorted(self.auth_dir.glob(self._glob)):
            try:
                key = _read_key_file(path)
            except (OSError, ValueError, TypeError):
                continue
            if not key:
                continue
            out.append({
                "ref": account_ref(key),
                "key": key,
                "path": path,
                "name": account_name(self.brand, key),
                "key_tail": str(key)[-4:],
                "source": "",
            })
        return out

    def candidate(self, row: dict) -> AccountCandidate:
        return AccountCandidate(row["ref"], KeyAccount(row["ref"], row["key"], row["path"]))


    @property
    def _accounts(self) -> dict:
        """ref -> KeyAccount, the object candidates() hand the request path.

        A view over the scanned rows, so no caller has to learn the base
        class to ask the pool what it holds.
        """
        return {ref: KeyAccount(ref, row["key"], row["path"])
                for ref, row in self._rows.items()}


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
        """Store one key in the pool, primary if it is the first."""
        key = str(key or "").strip()
        if not key:
            raise ValueError("empty key")
        ref = account_ref(key)
        destination = self.auth_dir / (self.brand + "-" + ref + ".json")
        if destination.resolve().parent != self.auth_dir:
            raise RuntimeError("account file escapes the local auths dir")
        atomic_json(destination, {"brand": self.brand, "key": key,
                                   "source": str(source or "")[:120],
                                   "added_at": iso_timestamp(time.time())})
        self.reload()
        with self._lock:
            state = self._state_of(ref)
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

    def remove(self, ref: str) -> None:
        """Delete one key file, refusing anything the pool does not own.

        The check is on the path the *pool* recorded, not on a handle a caller
        may have swapped out from under it: a key whose file was moved
        somewhere else is left where it is, because the pool's auths/ is the
        only place it is allowed to delete from.
        """
        with self._lock:
            row = self._rows.get(ref)
            if row is None:
                raise KeyError(ref)
            resolved = row["path"].resolve()
            if resolved.parent != self.auth_dir:
                raise RuntimeError("account file is not inside the local auths dir")
            resolved.unlink(missing_ok=True)
            self.reload()


    def account_row(self, row: dict, state: dict) -> dict:
        entry = super().account_row(row, state)
        entry.update({
            "key_tail": row.get("key_tail") or "",
            "source": state.get("source") or "",
        })
        return entry


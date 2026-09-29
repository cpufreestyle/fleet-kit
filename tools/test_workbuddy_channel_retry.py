"""WorkBuddy non-streaming must survive an upstream channel rejection.

Measured 2026-09-29 on the -gpt variant, which keeps exactly one account in the
pool: the upstream answers every non-streaming request with error 11128
"unapproved channel". ``_collect_with_pool`` answered that with ``raise
_ChannelRetry()`` -- but ``_ChannelRetry`` was never defined anywhere in the
bridge, so the first channel rejection surfaced as an unhandled ``NameError``
inside the route instead of the intended resend. On top of that, the retry
re-inserted the candidate at the head of the candidate list while the
"retry N/3" counter lived inside the loop body, so the counter reset on every
re-entry: even with the exception defined, one account would have been resent
forever.

The counter now sits next to the candidate list (one bound per request, the same
contract ``_stream_with_pool`` already had) and the exception class exists.
These tests pin both halves.
"""
import asyncio
import importlib.util
import os
import sys
import time
import unittest
from unittest import mock

import httpx

BRIDGE_DIR = os.path.abspath(os.path.join(
    os.path.dirname(__file__), os.pardir, "bridges", "workbuddy"))
sys.path.insert(0, BRIDGE_DIR)

spec = importlib.util.spec_from_file_location(
    "workbuddy_core", os.path.join(BRIDGE_DIR, "core.py"))
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)

CHANNEL_ERROR = b'{"error":{"code":11128,"message":"unapproved channel"}}'
PLAIN_ERROR = b'{"error":{"message":"boom"}}'


class _Response:
    def __init__(self, status_code, raw):
        self.status_code = status_code
        self._raw = raw

    async def aread(self):
        return self._raw


class _StreamCM:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc):
        return False


class _FakeClient:
    """Canned httpx.AsyncClient: replays one scripted upstream response."""

    def __init__(self, status_code, raw):
        self.status_code = status_code
        self.raw = raw

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method, url, headers=None, json=None):
        return _StreamCM(_Response(self.status_code, self.raw))


class _Manager:
    def get_headers(self):
        return {"Authorization": "Bearer test"}


class _Candidate:
    ref = "cand-1"
    manager = _Manager()


class _Pool:
    def __init__(self, candidates):
        self._candidates = list(candidates)
        self.failures = []
        self.successes = []

    def candidates(self):
        return list(self._candidates)

    def mark_failure(self, ref, reason, cooldown_seconds):
        self.failures.append((ref, reason, cooldown_seconds))

    def mark_success(self, ref):
        self.successes.append(ref)


def _run(client_cls, pool):
    sleeps = []

    async def _sleep(delay):
        sleeps.append(delay)

    async def _collect():
        return await core._collect_with_pool(
            "http://upstream.invalid/v2/chat/completions", pool,
            {"model": "m", "stream": False}, "m", "r-1")

    with mock.patch.object(core.httpx, "AsyncClient", client_cls), \
            mock.patch.object(core.asyncio, "sleep", _sleep):
        try:
            asyncio.run(_collect())
        except core.HTTPException as exc:
            return exc.status_code, sleeps, exc
    return None, sleeps, None


class ChannelRetryTest(unittest.TestCase):
    def test_exception_class_is_defined(self):
        self.assertTrue(issubclass(core._ChannelRetry, Exception))

    def test_persistent_channel_error_terminates_and_marks_cooldown(self):
        pool = _Pool([_Candidate()])
        status, sleeps, exc = _run(lambda *a, **k: _FakeClient(403, CHANNEL_ERROR), pool)
        self.assertEqual(status, 403)
        self.assertEqual(len(sleeps), 3, "retry budget must be capped at 3")
        self.assertEqual(sleeps, [1.5, 3.0, 4.5])
        self.assertEqual(pool.failures, [("cand-1", "上游渠道校验未通过", 20)])
        self.assertEqual(pool.successes, [])

    def test_channel_budget_is_per_request_not_per_candidate(self):
        pool = _Pool([_Candidate(), _Candidate()])
        status, sleeps, exc = _run(lambda *a, **k: _FakeClient(403, CHANNEL_ERROR), pool)
        self.assertEqual(status, 403)
        self.assertEqual(len(sleeps), 3, "adding a candidate must not reset the budget")

    def test_non_channel_error_still_surfaces_upstream_body(self):
        pool = _Pool([_Candidate()])
        status, sleeps, exc = _run(lambda *a, **k: _FakeClient(429, PLAIN_ERROR), pool)
        self.assertEqual(status, 429)
        self.assertEqual(sleeps, [])
        self.assertEqual(pool.failures, [("cand-1", "账号触发限流", 60)])


if __name__ == "__main__":
    unittest.main()

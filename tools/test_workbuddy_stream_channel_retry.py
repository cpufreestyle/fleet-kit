"""WorkBuddy streaming must resend in place after an upstream channel rejection.

The streaming half of the 11128 handling had two control-flow defects the
non-streaming half did not. ``_stream_upstream`` ended its channel-rejection
branch in ``break``, and that ``break`` sat inside ``async with client.stream()``
which itself sat inside ``for attempt in range(4)`` -- so it unwound both context
managers and then left the *attempt* loop, abandoning the account and moving to
the next candidate, straight after a log line claiming "retry 1/3". Verified on a
standalone reproducer: a ``break`` in that shape never reaches iteration 1 of the
enclosing loop, while a ``continue`` does.

Separately, ``channel_attempts`` was reset inside ``for candidate in
candidates:``, so each account got its own three-resend budget. The non-streaming
path already bound that budget per request (see test_workbuddy_channel_retry.py),
and the comment in it explicitly claims the stream path shares the contract. A
persistent rejection against a ten-account pool would have spent 90 seconds and
40 requests on one user turn.

Fixed: ``continue`` inside the context managers, and the counter hoisted next to
the candidate list. These tests pin the resend (which account's headers reach the
fake client, how often) and the shared budget.
"""
import asyncio
import importlib.util
import os
import sys
import unittest
from unittest import mock

BRIDGE_DIR = os.path.abspath(os.path.join(
    os.path.dirname(__file__), os.pardir, "bridges", "workbuddy"))
sys.path.insert(0, BRIDGE_DIR)

spec = importlib.util.spec_from_file_location(
    "workbuddy_core_stream", os.path.join(BRIDGE_DIR, "core.py"))
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)

URL = "http://upstream.invalid/v2/chat/completions"
CHANNEL_ERROR = b'{"error":{"code":11128,"message":"unapproved channel"}}'
CHANNEL_REASON = "\u4e0a\u6e38\u6e20\u9053\u6821\u9a8c\u672a\u901a\u8fc7"


class _Response:
    """Just enough httpx.Response for the non-200 branch of _stream_upstream."""

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


class _RecordingClient:
    """Replays one canned non-200 reply and records the Authorization header.

    ``_stream_upstream`` builds ``httpx.AsyncClient(timeout=None)`` afresh for
    every attempt, so the recording log has to live on the class rather than on
    the instance -- a per-instance list would only ever see one entry.
    """

    seen = None  # rebound per test

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
        _RecordingClient.seen.append(headers.get("Authorization"))
        return _StreamCM(_Response(self.status_code, self.raw))


class _Manager:
    def __init__(self, token):
        self._token = token

    def get_headers(self):
        return {"Authorization": "Bearer " + self._token}


class _Candidate:
    def __init__(self, ref, token):
        self.ref = ref
        self.manager = _Manager(token)


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


async def _no_sleep(delay):
    return None


def _drain(pool):
    """Run one streaming request; return (chunks, seen, sleeps)."""
    seen = []
    sleeps = []

    async def _sleep(delay):
        sleeps.append(delay)

    async def _run():
        return [chunk async for chunk in core._stream_upstream(
            URL, pool, {"model": "m", "stream": True}, "m", 0.0, "r-1")]

    _RecordingClient.seen = seen
    with mock.patch.object(
            core.httpx, "AsyncClient",
            lambda *a, **k: _RecordingClient(403, CHANNEL_ERROR)), \
            mock.patch.object(core.asyncio, "sleep", _sleep):
        chunks = asyncio.run(_run())
    return chunks, seen, sleeps


class StreamChannelRetryTest(unittest.TestCase):
    def test_channel_error_resends_the_same_account_three_times(self):
        pool = _Pool([_Candidate("cand-1", "A")])
        chunks, seen, sleeps = _drain(pool)
        # Four requests on one account: the first three are the resends, the
        # fourth is the one that finds the budget spent and records the cooldown.
        self.assertEqual(seen, ["Bearer A"] * 4)
        self.assertEqual(sleeps, [1.5, 3.0, 4.5])
        self.assertEqual(pool.failures, [("cand-1", CHANNEL_REASON, 20)])
        self.assertEqual(pool.successes, [])
        self.assertTrue(chunks, "an error event must still reach the client")

    def test_resend_budget_is_per_request_not_per_candidate(self):
        """The stream path must share the non-streaming per-request budget."""
        pool = _Pool([_Candidate("cand-1", "A"), _Candidate("cand-2", "B")])
        chunks, seen, sleeps = _drain(pool)
        # Before the hoist this was ["Bearer A"] * 4 + ["Bearer B"] * 4, with
        # sleeps [1.5, 3.0, 4.5] repeated -- one budget per account.
        self.assertEqual(seen, ["Bearer A"] * 4 + ["Bearer B"])
        self.assertEqual(sleeps, [1.5, 3.0, 4.5])
        self.assertEqual(pool.failures,
                         [("cand-1", CHANNEL_REASON, 20),
                          ("cand-2", CHANNEL_REASON, 20)])

    def test_transient_channel_error_recovers_on_the_resend(self):
        """A 11128 followed by a clean SSE stream must produce output."""
        good = (b'data: {"choices":[{"delta":{"content":"hi"}]}\n\n',
                b"data: [DONE]\n\n")

        class _OkResponse:
            status_code = 200

            async def aread(self):
                return b""

            async def aiter_bytes(self):
                for part in good:
                    yield part

        class _OkCM:
            async def __aenter__(self):
                return _OkResponse()

            async def __aexit__(self, *exc):
                return False

        script = [_StreamCM(_Response(403, CHANNEL_ERROR)), _OkCM()]
        state = {"i": 0}

        class _ScriptedClient:
            def __call__(self, *a, **k):
                return self

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def stream(self, method, url, headers=None, json=None):
                cm = script[min(state["i"], len(script) - 1)]
                state["i"] += 1
                return cm

        pool = _Pool([_Candidate("cand-1", "A")])

        async def _run():
            return [chunk async for chunk in core._stream_upstream(
                URL, pool, {"model": "m", "stream": True}, "m", 0.0, "r-1")]

        with mock.patch.object(core.httpx, "AsyncClient", _ScriptedClient()), \
                mock.patch.object(core.asyncio, "sleep", _no_sleep):
            chunks = asyncio.run(_run())

        self.assertEqual(state["i"], 2, "the resend must actually be issued")
        self.assertTrue(any(b"hi" in c for c in chunks))
        self.assertTrue(any(b"[DONE]" in c for c in chunks))
        self.assertEqual(pool.successes, ["cand-1"])


if __name__ == "__main__":
    unittest.main()

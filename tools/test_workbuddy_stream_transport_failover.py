"""WorkBuddy streaming must fail over to the next account on a transport error.

`_stream_upstream`'s `except httpx.HTTPError` branch ended in yield + return for
*every* transport error, including ones raised before the first byte ever reached
the client -- the one window where retrying is still safe, and the window the
function's own docstring claims to cover ("account failover is deliberately
limited to ... transport errors ... before the first event is released"). A single
ConnectError against a ten-account pool therefore collapsed the whole turn into a
502 SSE error event while nine healthy accounts sat unused, while
`_collect_with_pool` -- the non-streaming half of the same route, with its own
test file -- happily failed over.

The three statements meant to implement that failover (`_refresh_pin_async()`,
`pool.mark_failure(...)`, the `_log(...)`) did sit in the file, but *after* the
except block instead of inside it. Every path out of the `try` is a
break/continue/return, so a break inside the try leaves the attempt loop
outright and those three lines -- one of which references the exc name that only
the handler binds -- were unreachable. Same shape as the RC34 dead code in
pin_shim_base_url.py, except the name guard cannot see this one: exc *is* defined
somewhere in the function scope, just never when the orphaned lines would run.

Fixed by moving the refresh/mark/log into the not-started arm of the handler.
These tests pin both arms: failover before the first byte, and the
no-duplication rule after it.
"""
import asyncio
import importlib.util
import os
import sys
import unittest
from unittest import mock

import httpx

BRIDGE_DIR = os.path.abspath(os.path.join(
    os.path.dirname(__file__), os.pardir, "bridges", "workbuddy"))
sys.path.insert(0, BRIDGE_DIR)

spec = importlib.util.spec_from_file_location(
    "workbuddy_core_stream_transport", os.path.join(BRIDGE_DIR, "core.py"))
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)

URL = "http://upstream.invalid/v2/chat/completions"
GOOD_SSE = (b'data: {"choices":[{"delta":{"content":"hi"},"finish_reason":null}]}\n\n'
            b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
            b'"usage":{"total_tokens":7}}\n\n'
            b'data: [DONE]\n\n')


class _Response:
    """A 200 upstream response streaming canned SSE bytes."""

    status_code = 200

    def __init__(self, chunks, drop_after=None):
        self._chunks = chunks
        self._drop_after = drop_after

    async def aiter_bytes(self):
        if self._drop_after is not None:
            yield self._chunks[0]
            raise self._drop_after
        for chunk in self._chunks:
            yield chunk


class _StreamCM:
    """Async context manager yielding a 200 response or raising on enter."""

    def __init__(self, enter=None, chunks=None, drop_after=None):
        self._enter = enter
        self._chunks = chunks
        self._drop_after = drop_after

    async def __aenter__(self):
        if self._enter is not None:
            raise self._enter
        return _Response(self._chunks, self._drop_after)

    async def __aexit__(self, *exc):
        return False

class _Client:
    """Fake httpx.AsyncClient dispatching on the Authorization header."""

    behaviour = None  # {token: ("connect",) | ("ok",) | ("mid_drop",)}

    def __init__(self, *args, **kwargs):
        pass

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method, url, headers=None, json=None):
        token = (headers or {}).get("Authorization", "")
        kind = _Client.behaviour.get(token, ("ok",))
        if kind[0] == "connect":
            return _StreamCM(enter=httpx.ConnectError("refused"))
        if kind[0] == "mid_drop":
            return _StreamCM(chunks=[GOOD_SSE],
                             drop_after=httpx.ConnectError("reset by peer"))
        return _StreamCM(chunks=[GOOD_SSE])


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


def _drain(pool):
    async def _run():
        return [chunk async for chunk in core._stream_upstream(
            URL, pool, {"model": "m", "stream": True}, "m", 0.0, "r-1")]
    with mock.patch.object(core.httpx, "AsyncClient", _Client):
        return asyncio.run(_run())


class StreamTransportFailoverTest(unittest.TestCase):
    def test_connect_error_before_first_byte_fails_over_to_next_account(self):
        pool = _Pool([_Candidate("acct-1", "t1"), _Candidate("acct-2", "t2")])
        _Client.behaviour = {"Bearer t1": ("connect",)}
        chunks = _drain(pool)
        joined = b"".join(chunks)
        self.assertIn(b"hi", joined,
                      "account 2 stream must be relayed after account 1 "
                      "refused the connection")
        self.assertIn("acct-1", [f[0] for f in pool.failures])
        self.assertIn("acct-2", pool.successes)

    def test_connect_error_after_first_byte_yields_one_error_event(self):
        pool = _Pool([_Candidate("acct-1", "t1")])
        _Client.behaviour = {"Bearer t1": ("mid_drop",)}
        chunks = _drain(pool)
        self.assertEqual(len(chunks), 2)
        self.assertTrue(chunks[0].startswith(b"data: {"))
        self.assertIn(b"upstream_error", chunks[1])
        self.assertEqual(pool.failures, [],
                         "a stream that already sent bytes must not be retried, "
                         "so the account must not be marked failed")

    def test_every_account_cooling_down_still_errors(self):
        pool = _Pool([])
        chunks = _drain(pool)
        self.assertIn(b"cooling down", chunks[0])


if __name__ == "__main__":
    unittest.main()

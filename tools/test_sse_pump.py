"""sse_pump must decode, not relay raw bytes.

The bridges relay an upstream SSE body while dropping the upstream's headers.
If the upstream gzip-encodes that body, forwarding aiter_raw() ships
compressed bytes with no Content-Encoding for the client to act on, so Codex
parses a stream that is not UTF-8 text at all. These tests pin the decoding
behaviour and the always-close guarantee.
"""
import asyncio
import gzip
import importlib.util
import os

spec = importlib.util.spec_from_file_location(
    "_common", os.path.join(os.path.dirname(__file__), os.pardir,
                            "bridges", "_common.py"))
_common = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_common)


class FakeResponse:
    """Stands in for httpx.Response: only what sse_pump actually touches."""

    def __init__(self, decoded):
        self.decoded = decoded
        self.closed = False

    async def aiter_bytes(self):
        for i in range(0, len(self.decoded), 8):
            yield self.decoded[i:i + 8]

    async def aiter_raw(self):
        raise AssertionError("sse_pump must not relay raw (still encoded) bytes")
        yield  # unreachable: keeps this an async generator, like httpx's

    async def aclose(self):
        self.closed = True


def _drain(resp):
    out = b""

    async def run():
        nonlocal out
        async for chunk in _common.sse_pump(resp):
            out += chunk

    asyncio.run(run())
    return out


def test_relayed_body_is_the_decoded_stream():
    payload = b'data: {"x":1}\n\ndata: [DONE]\n\n'
    resp = FakeResponse(payload)
    out = _drain(resp)
    assert out == payload
    assert resp.closed is True


def test_a_gzipped_upstream_still_arrives_as_plain_sse():
    # aiter_bytes is httpx's decoded view, so that is what must be forwarded.
    plain = b'data: {"x":1}\n\ndata: [DONE]\n\n'
    resp = FakeResponse(plain)
    out = _drain(resp)
    assert out.startswith(b"data: ")
    assert out[:2] != gzip.compress(b"x")[:2]


def test_upstream_is_released_when_the_client_disconnects():
    payload = b"data: " + b"x" * 200 + b"\n\n"
    resp = FakeResponse(payload)

    async def run():
        agen = _common.sse_pump(resp)
        await agen.__anext__()      # client took one chunk, then went away
        await agen.aclose()

    asyncio.run(run())
    assert resp.closed is True

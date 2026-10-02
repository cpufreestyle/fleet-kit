"""Loopback hub traffic must never travel through the macOS system proxy.

Measured 2026-09-29: macOS publishes its HTTP proxy (MacPacket on :1082) in
System Settings, not env vars, so every Python process inherits it. scutil
--proxy exempts localhost and the RFC1918 ranges but NOT 127.0.0.1, and
websockets answers the bypass question for "host:port", so a loopback hub
URL -- exactly what ~/.cline/data/locks/hub/production.json holds -- was
tunnelled through that proxy. When MacPacket answered CONNECT with garbage,
websockets raised InvalidProxyMessage in 0.01s, the bridge answered 502 and
the verifier read UPSTREAM_DOWN: a healthy hub with 14 working models filed
as dead.

These tests pin the one-line guard: a loopback hub is direct, a remote hub
still honours the system proxy.
"""
import asyncio
import importlib.util
import os
import types

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "cline_bridge",
    os.path.join(HERE, os.pardir, "bridges", "cline", "cline_bridge.py"))
cb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cb)


@pytest.mark.parametrize("url", [
    "ws://127.0.0.1:25463/hub",
    "ws://127.0.4.9:25463/hub",
    "ws://localhost:25463/hub",
    "ws://[::1]:25463/hub",
])
def test_a_loopback_hub_skips_the_system_proxy(url):
    assert cb.hub_proxy(url) is None


def test_a_remote_hub_still_honours_the_system_proxy():
    assert cb.hub_proxy("ws://hub.example.internal:25463/hub") is True


def test_the_hub_client_passes_that_decision_to_websockets(monkeypatch):
    seen = {}

    async def fake_connect(url, **kw):
        seen["url"] = url
        seen.update(kw)

        class WS:
            async def close(self):
                pass

        return WS()

    async def no_register(self):
        pass

    monkeypatch.setattr(cb, "websockets",
                         types.SimpleNamespace(connect=fake_connect))
    monkeypatch.setattr(cb.HubClient, "register", no_register)
    client = cb.HubClient("ws://127.0.0.1:25463/hub", "tok", "cid")
    asyncio.run(client.__aenter__())
    assert seen["url"] == "ws://127.0.0.1:25463/hub"
    assert seen["proxy"] is None

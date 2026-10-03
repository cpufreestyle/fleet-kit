"""Both Google bridges must be able to say which exit they are using.

2026-10-02 the whole Google fleet looked like an account problem -- 403 VALI on
gemini, the same on antigravity -- while the machine's system proxy (MacPacket)
had no international route at all. urllib picks that proxy up from the macOS
system configuration, so the bridge reports "unreachable" without telling the
operator where the request went.

GEMINI_UPSTREAM_PROXY / ANTIGRAVITY_UPSTREAM_PROXY now pin an explicit exit and
/health reports the effective one. These tests pin that contract.
"""

import importlib.util
import os
import sys

import pytest

BRIDGES = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, "bridges"))
sys.path.insert(0, BRIDGES)


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(BRIDGES, rel))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class _Probe:
    """Records how the bridge reached the socket, instead of reaching it."""

    def __init__(self):
        self.handlers = []
        self.opened = []
        self.urlopen_calls = []

    def build_opener(self, handler):
        self.handlers.append(handler)
        return self

    def open(self, req, timeout=None):
        self.opened.append((req, timeout))
        return self

    def urlopen(self, req, timeout=None):
        self.urlopen_calls.append((req, timeout))
        return self


@pytest.fixture(scope="module")
def google_bridges():
    return {
        "gemini": (_load("gemini_bridge_proxy", os.path.join("gemini", "gemini_bridge.py")),
                   "GEMINI_UPSTREAM_PROXY"),
        "antigravity": (_load("antigravity_bridge_proxy",
                              os.path.join("antigravity", "antigravity_bridge.py")),
                        "ANTIGRAVITY_UPSTREAM_PROXY"),
    }


@pytest.mark.parametrize("which", ["gemini", "antigravity"])
def test_exit_pin_reports_env_source(google_bridges, which, monkeypatch):
    mod, env = google_bridges[which]
    monkeypatch.setenv(env, "http://127.0.0.1:7890")
    assert mod.proxy_info() == {"proxy": "http://127.0.0.1:7890", "source": "env"}


@pytest.mark.parametrize("which", ["gemini", "antigravity"])
def test_no_pin_falls_back_to_system_or_direct(google_bridges, which, monkeypatch):
    mod, env = google_bridges[which]
    monkeypatch.delenv(env, raising=False)
    info = mod.proxy_info()
    assert info["source"] in ("system", "direct")
    assert info["proxy"] is None or str(info["proxy"]).startswith("http")


@pytest.mark.parametrize("which", ["gemini", "antigravity"])
def test_pinned_exit_reaches_the_socket(google_bridges, which, monkeypatch):
    mod, env = google_bridges[which]
    monkeypatch.setenv(env, "http://127.0.0.1:7890")
    probe = _Probe()
    monkeypatch.setattr(mod.urllib.request, "build_opener", probe.build_opener)
    monkeypatch.setattr(mod.urllib.request, "urlopen", probe.urlopen)
    mod._urlopen("req", 5)
    assert probe.opened == [("req", 5)]
    assert probe.urlopen_calls == []
    assert probe.handlers[-1].proxies == {"http": "http://127.0.0.1:7890",
                                          "https": "http://127.0.0.1:7890"}


@pytest.mark.parametrize("which", ["gemini", "antigravity"])
def test_without_a_pin_the_stdlib_path_stays(google_bridges, which, monkeypatch):
    mod, env = google_bridges[which]
    monkeypatch.delenv(env, raising=False)
    probe = _Probe()
    monkeypatch.setattr(mod.urllib.request, "build_opener", probe.build_opener)
    monkeypatch.setattr(mod.urllib.request, "urlopen", probe.urlopen)
    mod._urlopen("req", 5)
    assert probe.urlopen_calls == [("req", 5)]
    assert probe.opened == []

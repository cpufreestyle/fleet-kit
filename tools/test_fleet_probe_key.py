"""Guards the bridge-key lookup that decides whether a live bridge is probed
as reachable or as an unauthenticated stranger.

catalog_filter.py drops a bridge's rows from the Codex picker whenever the
reachability probe calls it unreachable, so a key that cannot be found while the
bridge is perfectly healthy empties the picker. These tests pin the two places a
key may legitimately come from: fleet.env, then the service install.sh wrote.
"""

import importlib.util
from pathlib import Path

import fleet_platform

_spec = importlib.util.spec_from_file_location(
    "fleet_probe", str(Path(__file__).with_name("fleet_probe.py")))
fleet_probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fleet_probe)


def stub_service_definitions(monkeypatch, envs):
    monkeypatch.setattr(fleet_platform, "service_envs", lambda: envs)


def test_fleet_env_key_wins_over_the_service_definition(monkeypatch):
    stub_service_definitions(
        monkeypatch, {"com.local.xhx2codex": {"XHX2CODEX_KEY": "sk-wrapper"}})
    assert fleet_probe.bridge_key("xhx", {"XHX2CODEX_KEY": "sk-env"}) == "sk-env"


def test_service_definition_supplies_the_key_when_env_is_absent(monkeypatch):
    stub_service_definitions(
        monkeypatch, {"com.local.xhx2codex": {"XHX2CODEX_KEY": "sk-wrapper"}})
    assert fleet_probe.bridge_key("xhx", {}) == "sk-wrapper"


def test_no_service_definition_probes_without_a_key(monkeypatch):
    stub_service_definitions(monkeypatch, {})
    assert fleet_probe.bridge_key("xhx", {}) == ""


def test_unknown_bridge_probes_without_a_key(monkeypatch):
    stub_service_definitions(
        monkeypatch, {"com.local.xhx2codex": {"XHX2CODEX_KEY": "sk-wrapper"}})
    assert fleet_probe.bridge_key("nosuchbridge", {}) == ""


def test_unreadable_service_definitions_do_not_raise(monkeypatch):
    def boom():
        raise OSError("service dir unreadable")

    monkeypatch.setattr(fleet_platform, "service_envs", boom)
    assert fleet_probe.bridge_key("xhx", {}) == ""

"""The desktop bridge must rewrite a profile, never a user's other profiles.

What is checked here, and why each one earned a test:

* the location rules, because a bridge that writes ~/.config on macOS is a
  bridge that edits a file the app never reads, and the user sees nothing;
* that apply keeps every other configLibrary entry, because cc-switch is
  how the user gets back and losing it costs them their other gateways;
* that the entry it writes carries no inferenceModels list, because an
  explicit list makes the app skip model discovery and the picker collapses
  to whatever was hand-listed;
* that off and remove are reversible, because a bridge the user cannot step
  out of is not a bridge;
* that a dry run writes nothing, because --dry-run that still writes is a
  lie the user only finds out afterwards.

No test here touches the network: the gateway probe is the one thing that
would, so it is stubbed.
"""
import json
import os

import pytest

import claude_desktop_bridge as bridge

CC_SWITCH_ID = "00000000-0000-4000-8000-000000157210"
FLEETKIT_ID = "00000000-0000-4000-8000-000000088010"


def _library(tmp_path):
    """A configLibrary holding one cc-switch entry, the shape this machine has."""
    home = tmp_path / "home"
    library = home / "configLibrary"
    library.mkdir(parents=True)
    (library / (CC_SWITCH_ID + ".json")).write_text(json.dumps({
        "coworkEgressAllowedHosts": ["*"],
        "disableDeploymentModeChooser": True,
        "inferenceGatewayApiKey": "ccs-3a1a0809c8764ecdb93b5b1b52fffe09",
        "inferenceGatewayAuthScheme": "bearer",
        "inferenceGatewayBaseUrl": "http://127.0.0.1:15721/claude-desktop",
        "inferenceModels": [{"labelOverride": "claude-opus-5",
                            "name": "claude-opus-5"}],
        "inferenceProvider": "gateway",
    }), encoding="utf-8")
    (library / "_meta.json").write_text(json.dumps({
        "appliedId": CC_SWITCH_ID,
        "entries": [{"id": CC_SWITCH_ID, "name": "CC Switch"}],
    }), encoding="utf-8")
    return home


@pytest.fixture
def home(tmp_path):
    return str(_library(tmp_path))


@pytest.fixture
def no_probe(monkeypatch):
    """Keep the gateway probe off the wire; a status test is not an HTTP test."""
    monkeypatch.setattr(bridge, "probe_gateway",
                        lambda url, timeout=8.0: (200, ["a/b", "c/d"]))


# ------------------------------------------------------------------- locations
def test_entry_id_is_built_from_the_port_and_stays_valid():
    entry_id = bridge.default_entry_id(8801)
    assert entry_id == FLEETKIT_ID
    assert bridge.ENTRY_ID_RE.match(entry_id), "the app rejects this id"
    assert bridge.default_entry_id(15721) == CC_SWITCH_ID


def test_user_data_dir_on_darwin(monkeypatch):
    monkeypatch.delenv("CLAUDE_USER_DATA_DIR", raising=False)
    monkeypatch.setenv("HOME", "/home/tester")
    monkeypatch.setattr(bridge, "platform_name", lambda: "darwin")
    assert bridge.user_data_dir() == "/home/tester/Library/Application Support/Claude-3p"


def test_user_data_dir_on_linux(monkeypatch):
    monkeypatch.delenv("CLAUDE_USER_DATA_DIR", raising=False)
    monkeypatch.setenv("HOME", "/home/tester")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(bridge, "platform_name", lambda: "linux")
    assert bridge.user_data_dir() == "/home/tester/.config/Claude-3p"


def test_user_data_dir_on_windows(monkeypatch):
    monkeypatch.delenv("CLAUDE_USER_DATA_DIR", raising=False)
    local = "C:/Users/tester/AppData/Local"
    monkeypatch.setenv("LOCALAPPDATA", local)
    monkeypatch.setattr(bridge, "platform_name", lambda: "win32")
    assert bridge.user_data_dir() == local + "/Claude-3p"


def test_user_data_dir_honours_the_env_override(monkeypatch):
    monkeypatch.setenv("CLAUDE_USER_DATA_DIR", "/tmp/forced-3p")
    monkeypatch.setattr(bridge, "platform_name", lambda: "darwin")
    assert bridge.user_data_dir() == "/tmp/forced-3p"


def test_user_data_dir_prefers_an_explicit_home(monkeypatch):
    monkeypatch.setenv("CLAUDE_USER_DATA_DIR", "/tmp/forced-3p")
    monkeypatch.setattr(bridge, "platform_name", lambda: "linux")
    assert bridge.user_data_dir("/tmp/given") == "/tmp/given"


# ----------------------------------------------------------------------- entry
def test_the_entry_keeps_the_sandbox_fields_of_the_entry_it_replaces():
    entry = bridge.fleetkit_entry("http://127.0.0.1:8801", "sk-fleetkit-local",
                                 inherit={"coworkEgressAllowedHosts": ["*"],
                                          "disableDeploymentModeChooser": True})
    assert entry["inferenceProvider"] == "gateway"
    assert entry["inferenceGatewayBaseUrl"] == "http://127.0.0.1:8801"
    assert entry["inferenceGatewayAuthScheme"] == "bearer"
    assert entry["inferenceGatewayApiKey"] == "sk-fleetkit-local"
    assert entry["coworkEgressAllowedHosts"] == ["*"]
    assert entry["disableDeploymentModeChooser"] is True


def test_the_entry_lists_no_models_so_discovery_still_runs():
    entry = bridge.fleetkit_entry("http://127.0.0.1:8801", "k")
    assert "inferenceModels" not in entry
    assert "modelDiscoveryEnabled" not in entry


# ----------------------------------------------------------------------- apply
def test_apply_writes_the_entry_and_keeps_cc_switch(home):
    assert bridge.main(["apply", "--home", home]) == 0
    meta = json.loads(os.path.join(home, "configLibrary", "_meta.json")
                      and open(os.path.join(home, "configLibrary", "_meta.json"),
                               encoding="utf-8").read())
    assert meta["appliedId"] == FLEETKIT_ID
    assert [e["id"] for e in meta["entries"]] == [FLEETKIT_ID, CC_SWITCH_ID]
    entry = json.load(open(bridge.entry_path(FLEETKIT_ID, home), encoding="utf-8"))
    assert entry["inferenceGatewayBaseUrl"] == "http://127.0.0.1:8801"
    assert "inferenceModels" not in entry
    assert os.path.exists(bridge.entry_path(CC_SWITCH_ID, home)), \
        "the cc-switch entry must survive the bridge"


def test_apply_twice_does_not_duplicate_the_entry(home):
    bridge.main(["apply", "--home", home])
    bridge.main(["apply", "--home", home])
    meta = json.load(open(bridge.meta_path(home), encoding="utf-8"))
    ids = [e["id"] for e in meta["entries"]]
    assert ids.count(FLEETKIT_ID) == 1
    assert set(ids) == {FLEETKIT_ID, CC_SWITCH_ID}


def test_apply_backs_up_what_it_overwrites(home):
    bridge.main(["apply", "--home", home])
    library = os.path.join(home, "configLibrary")
    backups = [n for n in os.listdir(library) if ".bak-" in n]
    assert backups, "apply must leave a way back"
    assert any(n.startswith("_meta.json.bak-") for n in backups)


def test_a_dry_run_writes_nothing(home):
    before = open(bridge.meta_path(home), encoding="utf-8").read()
    assert bridge.main(["--dry-run", "apply", "--home", home]) == 0
    assert open(bridge.meta_path(home), encoding="utf-8").read() == before
    assert not os.path.exists(bridge.entry_path(FLEETKIT_ID, home))


def test_apply_refuses_an_id_the_app_would_reject(home, capsys):
    assert bridge.main(["apply", "--home", home, "--id", "not-a-uuid"]) == 2
    assert not os.path.exists(bridge.entry_path("not-a-uuid", home))


# -------------------------------------------------------------------- off/back
def test_off_steps_back_to_the_entry_that_was_applied(home):
    bridge.main(["apply", "--home", home])
    assert bridge.main(["off", "--home", home]) == 0
    meta = json.load(open(bridge.meta_path(home), encoding="utf-8"))
    assert meta["appliedId"] == CC_SWITCH_ID
    assert [e["id"] for e in meta["entries"]] == [FLEETKIT_ID, CC_SWITCH_ID]


def test_off_with_nothing_to_step_back_to_is_refused(home, capsys):
    meta_path = bridge.meta_path(home)
    json.dump({"appliedId": FLEETKIT_ID,
               "entries": [{"id": FLEETKIT_ID, "name": "FleetKit"}]},
              open(meta_path, "w", encoding="utf-8"))
    assert bridge.main(["off", "--home", home]) == 1
    assert json.load(open(meta_path, encoding="utf-8"))["appliedId"] == FLEETKIT_ID


def test_off_refuses_when_fleetkit_is_not_applied(home, capsys):
    assert bridge.main(["off", "--home", home]) == 1
    assert json.load(open(bridge.meta_path(home),
                          encoding="utf-8"))["appliedId"] == CC_SWITCH_ID


def test_remove_deletes_only_the_fleetkit_entry(home):
    bridge.main(["apply", "--home", home])
    assert bridge.main(["remove", "--home", home]) == 0
    assert not os.path.exists(bridge.entry_path(FLEETKIT_ID, home))
    assert os.path.exists(bridge.entry_path(CC_SWITCH_ID, home))
    meta = json.load(open(bridge.meta_path(home), encoding="utf-8"))
    assert meta["appliedId"] == CC_SWITCH_ID


def test_remove_without_an_entry_is_a_no_op(home, capsys):
    before = open(bridge.meta_path(home), encoding="utf-8").read()
    assert bridge.main(["remove", "--home", home]) == 0
    assert open(bridge.meta_path(home), encoding="utf-8").read() == before


# --------------------------------------------------------------------- status
def test_status_reports_the_applied_gateway_and_its_models(home, no_probe, capsys):
    bridge.main(["apply", "--home", home])
    assert bridge.main(["status", "--home", home]) == 0
    out = capsys.readouterr().out
    assert "http://127.0.0.1:8801" in out
    assert "2 models" in out
    assert "a/b" in out


def test_status_on_a_missing_library_says_so(home, no_probe, capsys):
    import shutil
    shutil.rmtree(os.path.join(home, "configLibrary"))
    assert bridge.main(["status", "--home", home]) == 1
    assert "no config library" in capsys.readouterr().out


def test_status_survives_a_gateway_that_does_not_answer(home, monkeypatch, capsys):
    bridge.main(["apply", "--home", home])
    monkeypatch.setattr(bridge, "probe_gateway",
                        lambda url, timeout=8.0: (None, "URLError: refused"))
    assert bridge.main(["status", "--home", home]) == 0
    assert "refused" in capsys.readouterr().out

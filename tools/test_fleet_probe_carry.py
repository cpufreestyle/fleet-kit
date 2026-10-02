"""A bridge the sweep cannot call must not be un-measured into invisibility.

Measured 2026-10-01: zcode/GLM-5.3-Flash answered a real chat completion
("OK", finish_reason stop) through the local bridge, yet the picker still
could not show it. fleet_probe.py lists zcode in NO_PROBE -- probing it burns
an Aliyun captcha ticket -- and the launchd timer rewrites the snapshot every
30 minutes without --merge, so the bridge was recorded as "skipped" on every
sweep. catalog_sort.py then tiered it unknown, which is below every reachable
provider, and zcode landed at row 67 of 117: present in the file, absent from
the first screen.

The fix is to carry a real-call verdict forward instead of erasing it, so a
deliberate `--only zcode` run keeps the bridge green until its proof ages out
without anyone re-burning a captcha.
"""
import datetime
import importlib.util
import json
import os
import sys

import pytest

KIT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
spec = importlib.util.spec_from_file_location(
    "fleet_probe_carry", os.path.join(KIT, "tools", "fleet_probe.py"))
fp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fp)

PROBE = "zcode"
NOW = datetime.datetime.now(datetime.timezone.utc)


def _snapshot(tmp_path, **over):
    path = tmp_path / "fleet-reach.json"
    snap = {
        "reachable": [PROBE],
        "unreachable": [],
        "skipped": {},
        "measured_at": NOW.isoformat(timespec="seconds"),
        "ports": {PROBE: 8800},
        "evidence": {PROBE: "GLM-5.3-Flash -> E2E_OK"},
        "verified_models": {PROBE: "GLM-5.3-Flash"},
        "verified_at": {PROBE: NOW.isoformat(timespec="seconds")},
    }
    snap.update(over)
    path.write_text(json.dumps(snap), encoding="utf-8")
    return path


def _run(capsys, monkeypatch, argv):
    """Run main() in-process against stubbed bridges, capturing stdout."""
    monkeypatch.setattr(sys, "argv", ["fleet_probe.py"] + argv)
    monkeypatch.setattr(fp, "probe",
                        lambda *a, **k: (True, "stub -> E2E_OK", "stub-model"))
    monkeypatch.setattr(
        fp, "probe_gateway",
        lambda *a, **k: (True, "stub -> E2E_OK", "stub-model"))
    monkeypatch.setattr(fp.subprocess, "run",
                        lambda *a, **k: type("R", (), {"stdout": "",
                                                       "stderr": ""})())
    fp.main()
    return capsys.readouterr()


def test_a_fresh_real_call_verdict_is_carried_forward(tmp_path):
    path = _snapshot(tmp_path)
    kept = fp.carried_verdict(PROBE, str(path))
    assert kept is not None, "a proven bridge must not be demoted to skipped"
    assert kept["model"] == "GLM-5.3-Flash"
    assert "E2E_OK" in kept["evidence"]


def test_an_aged_verdict_is_not_evidence(tmp_path):
    """A proof from three days ago says nothing about today."""
    old = (NOW - datetime.timedelta(hours=72)).isoformat(timespec="seconds")
    path = _snapshot(tmp_path, verified_at={PROBE: old})
    assert fp.carried_verdict(PROBE, str(path)) is None


@pytest.mark.parametrize("patch", [
    {"reachable": []},
    {"verified_models": {}},
    {"verified_at": {}},
    {"verified_at": {PROBE: "not-a-timestamp"}},
])
def test_nothing_is_carried_without_a_real_recent_proof(tmp_path, patch):
    path = _snapshot(tmp_path, **patch)
    assert fp.carried_verdict(PROBE, str(path)) is None


def test_a_missing_snapshot_is_not_an_error(tmp_path):
    assert fp.carried_verdict(PROBE, str(tmp_path / "absent.json")) is None


def test_the_routine_sweep_keeps_a_proven_bridge_green(tmp_path, monkeypatch, capsys):
    """No --only, so the sweep is the 30-minute timer run."""
    out = _snapshot(tmp_path)
    env = tmp_path / "fleet.env"
    env.write_text("ZCODE2CODEX_KEY=sk-test\n", encoding="utf-8")
    before = json.loads(out.read_text(encoding="utf-8"))

    out2, _err = _run(capsys, monkeypatch,
                     ["--env", str(env), "--out", str(out), "--stdout"])
    snap = json.loads(out2[out2.index("{"):])

    assert before["skipped"] == {}, "the fixture should not pre-skip zcode"
    assert PROBE in snap["reachable"], (
        "the sweep demoted a bridge it never called back to skipped")
    assert PROBE not in snap.get("skipped", {})
    assert "carried forward" in snap["evidence"][PROBE]
    assert snap["verified_models"][PROBE] == "GLM-5.3-Flash"
    # the inherited proof keeps its own stamp, so it ages on its own clock
    assert snap["verified_at"][PROBE] == before["verified_at"][PROBE]


def test_without_a_verdict_the_sweep_still_skips(tmp_path, monkeypatch, capsys):
    """The carry-forward is an optimisation, not a reason to start probing."""
    out = tmp_path / "fleet-reach.json"
    out.write_text(json.dumps({
        "reachable": ["workbuddy"], "unreachable": [], "skipped": {},
        "measured_at": NOW.isoformat(timespec="seconds"),
        "verified_models": {}, "verified_at": {}}), encoding="utf-8")
    env = tmp_path / "fleet.env"
    env.write_text("ZCODE2CODEX_KEY=sk-test\n", encoding="utf-8")

    stdout, _err = _run(capsys, monkeypatch,
                     ["--env", str(env), "--out", str(out), "--stdout"])
    snap = json.loads(stdout[stdout.index("{"):])

    assert fp.NO_PROBE.get(PROBE), "zcode must stay opt-in"
    assert PROBE in snap["skipped"], "an unproven bridge is still skipped"
    assert PROBE not in snap["reachable"]


def test_a_deliberate_probe_still_runs_and_stamps_now(tmp_path, monkeypatch, capsys):
    """--only bypasses NO_PROBE and produces a fresh, own-clock proof."""
    out = _snapshot(tmp_path)
    env = tmp_path / "fleet.env"
    env.write_text("ZCODE2CODEX_KEY=sk-test\n", encoding="utf-8")

    stdout, _err = _run(capsys, monkeypatch,
                     ["--env", str(env), "--out", str(out), "--stdout",
                      "--only", PROBE])
    snap = json.loads(stdout[stdout.index("{"):])

    assert PROBE in snap["reachable"]
    assert snap["verified_at"][PROBE] == snap["measured_at"]
    assert "carried forward" not in snap["evidence"][PROBE]


def test_merge_keeps_the_proof_of_bridges_it_did_not_call(tmp_path,
                                                          monkeypatch, capsys):
    """A --only run must not blank everyone else's verified_models.

    catalog_sort.py uses verified_models to float proven-good rows up, so
    losing it for the other providers silently degrades the whole ordering.
    Note --stdout short-circuits the merge, so this one lets it write.
    """
    out = _snapshot(tmp_path,
                    reachable=[PROBE, "workbuddy"],
                    verified_models={PROBE: "GLM-5.3-Flash",
                                     "workbuddy": "glm-5.2"},
                    verified_at={PROBE: NOW.isoformat(timespec="seconds"),
                                 "workbuddy": NOW.isoformat(timespec="seconds")})
    env = tmp_path / "fleet.env"
    env.write_text("ZCODE2CODEX_KEY=sk-test\n", encoding="utf-8")

    _run(capsys, monkeypatch,
         ["--env", str(env), "--out", str(out), "--merge", "--only", PROBE])
    snap = json.loads(out.read_text(encoding="utf-8"))

    assert snap["verified_models"].get("workbuddy") == "glm-5.2"
    assert snap["verified_at"].get("workbuddy")
    assert "workbuddy" in snap["reachable"]
    assert snap["verified_models"].get(PROBE) == "stub-model"

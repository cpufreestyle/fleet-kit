"""Can-write-code verdicts, end to end: the bench collapses one run into
FULL / PARTIAL / NORUN / DEAD, snapshots it atomically without blanking the
rows a single-bridge run did not touch, and free_models turns each snapshot
row into a badge the status panel can sort on.

Measured 2026-10-03 (kit/code-capability.json): five models pass both coding
tasks outright -- workbuddy's three deepseek v4 bodies and tokendance's flash
and pro over the same two tasks -- trae passes task1 but times out task2 (a
partial, not a pass), four cannot run the harness (codely-core + xhx), and
sixteen are dead bridges. A model that was never measured must read "?", never
a guess: several bridges expose deepseek-v4-pro under their own names, so a
bare model id is trusted only when exactly one fleet row ends with it.
"""
import importlib.util
import json
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(HERE, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


free_models = _load("free_models", "free_models.py")
bench = _load("code_model_bench", "code_model_bench.py")


def _full_task(name):
    return {"task": name, "score": [8, 8], "total": 8, "no_runnable": False,
            "score_str": "8/8", "prose": 0, "failures": []}


def _short_task(name):
    return {"task": name, "total": 8, "no_runnable": False,
            "score_str": "5/8", "prose": 2, "failures": ["check 3"]}


def _prose_task(name):
    return {"task": name, "total": 8, "no_runnable": True,
            "score_str": "", "prose": 12, "failures": []}


def _res(reachable=True, tasks=None, error=None, model="m", bridge="b"):
    return {"model": model, "bridge": bridge, "reachable": reachable,
            "smoke": None, "total_seconds": 1.0, "error": error,
            "tasks": [] if tasks is None else tasks}


BOTH = [_full_task(1), _full_task(2)]


def test_verdict_full_only_when_every_check_passes():
    assert bench.verdict_of(_res(tasks=BOTH)) == "FULL"


def test_verdict_dead_when_the_bridge_is_unreachable():
    assert bench.verdict_of(_res(reachable=False, tasks=BOTH)) == "DEAD"


def test_verdict_dead_when_no_task_produced_evidence():
    assert bench.verdict_of(_res(tasks=[])) == "DEAD"


def test_verdict_norun_when_the_reply_was_prose_only():
    res = _res(tasks=[_prose_task(1), _prose_task(2)])
    assert bench.verdict_of(res) == "NORUN"


def test_verdict_partial_when_a_task_never_finished():
    # the 2026-10-03 trae run passed task1 then timed out task2
    assert bench.verdict_of(_res(tasks=[_full_task(1)])) == "PARTIAL"


def test_verdict_partial_when_a_check_was_missed():
    res = _res(tasks=[_full_task(1), _short_task(2)])
    assert bench.verdict_of(res) == "PARTIAL"


def test_every_verdict_has_a_panel_badge():
    assert set(bench.VERDICTS) <= set(free_models.CODE_BADGE)
    assert free_models.CODE_BADGE[None] == "?"


def test_slug_key_prefixes_the_bridge_when_given():
    assert (bench.slug_key("deepseek-v4-flash", "workbuddy")
            == "workbuddy/deepseek-v4-flash")


def test_slug_key_keeps_a_bare_gateway_model():
    assert bench.slug_key("gpt-5.5") == "gpt-5.5"


def test_snapshot_merges_and_keeps_unmeasured_rows(tmp_path):
    out = tmp_path / "cap.json"
    out.write_text(json.dumps(
        {"models": {"wb/old-dead": {"verdict": "DEAD"}}}), encoding="utf-8")
    snap = bench.record_snapshot(
        [_res(model="deepseek-v4-flash", bridge="workbuddy", tasks=BOTH)],
        out=str(out))
    rows = snap["models"]
    assert "wb/old-dead" in rows
    assert rows["workbuddy/deepseek-v4-flash"]["verdict"] == "FULL"
    assert not os.path.exists(str(out) + ".tmp")
    disk = json.loads(out.read_text(encoding="utf-8"))
    assert disk["models"]["workbuddy/deepseek-v4-flash"]["verdict"] == "FULL"


def test_snapshot_survives_a_corrupt_prior_file(tmp_path):
    out = tmp_path / "cap.json"
    out.write_text("{ not valid json ", encoding="utf-8")
    snap = bench.record_snapshot(
        [_res(model="m", bridge="b", tasks=BOTH)], out=str(out))
    assert snap["models"]["b/m"]["verdict"] == "FULL"


def test_lookup_prefers_the_exact_provider_model():
    cap = {"workbuddy/deepseek-v4-flash": {"verdict": "FULL"},
           "tokendance/deepseek-v4-flash": {"verdict": "PARTIAL"}}
    assert free_models.code_cap_lookup(
        cap, "workbuddy", "deepseek-v4-flash")["verdict"] == "FULL"


def test_lookup_accepts_a_same_provider_suffix():
    cap = {"workbuddy/abc-deepseek-v4-flash": {"verdict": "FULL"}}
    assert free_models.code_cap_lookup(
        cap, "workbuddy", "deepseek-v4-flash")["verdict"] == "FULL"


def test_lookup_refuses_an_ambiguous_bare_id():
    cap = {"workbuddy/deepseek-v4-pro": {"verdict": "FULL"},
           "tokendance/deepseek-v4-pro": {"verdict": "PARTIAL"}}
    assert free_models.code_cap_lookup(
        cap, "whatever", "deepseek-v4-pro") is None


def test_lookup_trusts_the_only_fleet_wide_match():
    cap = {"workbuddy/deepseek-v4-flash": {"verdict": "FULL"}}
    assert free_models.code_cap_lookup(
        cap, "other", "deepseek-v4-flash")["verdict"] == "FULL"


def test_lookup_never_guesses_a_slashed_id():
    cap = {"workbuddy/deepseek-v4-flash": {"verdict": "FULL"}}
    assert free_models.code_cap_lookup(
        cap, "other", "foo/deepseek-v4-flash") is None


def test_lookup_returns_none_for_an_empty_cap():
    assert free_models.code_cap_lookup({}, "workbuddy", "x") is None
    assert free_models.code_cap_lookup(None, "workbuddy", "x") is None


def test_age_days_measures_a_few_days_back():
    stamp = time.strftime("%Y-%m-%d %H:%M:%S",
                          time.localtime(time.time() - 3 * 86400))
    age = free_models.code_age_days(stamp)
    assert age is not None and 2.9 < age < 3.1


def test_age_days_is_none_for_unparseable_input():
    assert free_models.code_age_days(None) is None
    assert free_models.code_age_days("someday soon") is None


def test_annotate_carries_a_fresh_verdict():
    recent = time.strftime("%Y-%m-%d %H:%M:%S",
                           time.localtime(time.time() - 3600))
    cap = {"workbuddy/deepseek-v4-flash": {
        "verdict": "FULL", "total_seconds": 7.8, "measured_at": recent}}
    row = free_models.annotate(
        {"models": {}, "providers": {}}, "workbuddy", "deepseek-v4-flash", cap)
    assert row["code_verdict"] == "FULL"
    assert row["code_badge"] == "全过"
    assert row["code_seconds"] == 7.8
    assert row["code_stale"] is False


def test_annotate_flags_a_stale_measurement():
    old = time.strftime("%Y-%m-%d %H:%M:%S",
                        time.localtime(time.time() - 9 * 86400))
    cap = {"workbuddy/deepseek-v4-flash": {"verdict": "FULL", "measured_at": old}}
    row = free_models.annotate(
        {"models": {}, "providers": {}}, "workbuddy", "deepseek-v4-flash", cap)
    assert row["code_stale"] is True


def test_annotate_unknown_model_shows_a_question_mark():
    row = free_models.annotate(
        {"models": {}, "providers": {}}, "workbuddy", "never-measured", {})
    assert row["code_verdict"] is None
    assert row["code_badge"] == "?"
    assert row["code_at"] is None
    assert row["code_stale"] is False


def test_build_carries_code_verdicts_and_counts(monkeypatch):
    recent = time.strftime("%Y-%m-%d %H:%M:%S",
                           time.localtime(time.time() - 3600))
    cap = {"workbuddy/deepseek-v4-flash": {
        "verdict": "FULL", "total_seconds": 7.8, "measured_at": recent}}
    monkeypatch.setattr(free_models, "load_db",
                        lambda: {"models": {}, "providers": {}})
    monkeypatch.setattr(free_models, "load_code_cap", lambda: cap)
    monkeypatch.setattr(free_models, "catalog_index", lambda: {})
    monkeypatch.setattr(free_models, "ocx_live",
                        lambda: [("workbuddy", "deepseek-v4-flash"),
                                 ("x", "unmeasured")])
    snap = free_models.build()
    by_model = {r["model"]: r for r in snap["models"]}
    assert by_model["workbuddy/deepseek-v4-flash"]["code_verdict"] == "FULL"
    assert by_model["x/unmeasured"]["code_verdict"] is None
    assert snap["code_counts"].get("FULL") == 1
    assert snap["code_counts"].get("none") == 1
    assert snap["code_stale_after_days"] == free_models.STALE_AFTER_DAYS


def test_load_code_cap_degrades_to_empty(tmp_path, monkeypatch):
    junk = tmp_path / "junk.json"
    junk.write_text("}{ broken", encoding="utf-8")
    monkeypatch.setattr(free_models, "CODE_CAP_PATH", str(junk))
    assert free_models.load_code_cap() == {}


def test_committed_snapshot_loads_with_valid_verdicts():
    cap = free_models.load_code_cap()
    assert cap, "kit/code-capability.json should load and be non-empty"
    for slug, entry in cap.items():
        assert entry.get("verdict") in bench.VERDICTS, slug
        assert entry.get("measured_at"), slug

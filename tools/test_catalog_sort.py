import datetime
import importlib.util
import os
import time

spec = importlib.util.spec_from_file_location(
    "catalog_sort", os.path.join(os.path.dirname(__file__), "catalog_sort.py"))
catalog_sort = importlib.util.module_from_spec(spec)
spec.loader.exec_module(catalog_sort)


def test_snapshot_age_reads_a_timestamp():
    now = datetime.datetime.now(datetime.timezone.utc)
    stamp = (now - datetime.timedelta(hours=2)).isoformat()
    age = catalog_sort.snapshot_age_seconds({"measured_at": stamp})
    assert 7000 < age < 7400


def test_snapshot_age_is_none_without_a_stamp():
    # A snapshot with no measurable age must be treated as unmeasurable so the
    # caller refuses to delete rows on the strength of it.
    assert catalog_sort.snapshot_age_seconds({}) is None
    assert catalog_sort.snapshot_age_seconds({"measured_at": "yesterday"}) is None


def test_snapshot_age_treats_naive_stamps_as_utc():
    stamp = (datetime.datetime.now(datetime.timezone.utc)
             - datetime.timedelta(minutes=30)).replace(tzinfo=None).isoformat()
    age = catalog_sort.snapshot_age_seconds({"measured_at": stamp})
    assert 1700 < age < 1900


def test_prune_backups_keeps_the_newest(tmp_path):
    catalog = tmp_path / "catalog.json"
    catalog.write_text("{}")
    for i in range(7):
        bak = tmp_path / ("catalog.json.bak-2026010%d-000000" % i)
        bak.write_text("{}")
        os.utime(bak, (time.time() - (7 - i) * 60,) * 2)

    removed = catalog_sort.prune_backups(str(catalog), keep=3)

    assert len(removed) == 4
    left = sorted(p.name for p in tmp_path.glob("catalog.json.bak-*"))
    assert left == ["catalog.json.bak-20260104-000000",
                    "catalog.json.bak-20260105-000000",
                    "catalog.json.bak-20260106-000000"]


def test_prune_backups_leaves_other_files_alone(tmp_path):
    catalog = tmp_path / "catalog.json"
    catalog.write_text("{}")
    (tmp_path / "models_cache.json").write_text("{}")
    (tmp_path / "models_cache.json.bak-20260101-000000").write_text("{}")

    assert catalog_sort.prune_backups(str(catalog), keep=0) == []
    assert (tmp_path / "models_cache.json").exists()
    assert (tmp_path / "models_cache.json.bak-20260101-000000").exists()


def test_bridged_providers_reads_the_platform_table():
    names = catalog_sort.bridged_providers()
    assert names is not None
    # The guard that limits deletion to unbridged providers depends on this
    # table; a provider losing its bridge would silently start being deletable.
    assert {"workbuddy", "trae", "zcode", "qwen"} <= names


def test_a_bridged_provider_is_never_dropped():
    # tokendance has no bridge and a dead key, so it is the one provider that
    # may be erased; a probed-dead bridge (workbuddy-gpt) must survive.
    bridged = catalog_sort.bridged_providers() or set()
    assert "tokendance" not in bridged
    assert "workbuddy-gpt" in bridged


def _sort(catalog, reach, *extra):
    import json
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, os.path.join(os.path.dirname(__file__),
                                      "catalog_sort.py"),
         "--catalog", str(catalog), "--reach", str(reach), "--no-backup",
         *extra],
        capture_output=True, text=True, timeout=60)
    return proc


def _catalog(tmp_path):
    import json
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"models": [
        {"slug": "workbuddy/glm-5.2"},
        {"slug": "zcode/GLM-5.3"},
    ]}))
    return catalog


def test_strict_coverage_ignores_a_deliberate_skip(tmp_path):
    # --strict-coverage refuses when the snapshot misses a catalog provider,
    # and zcode is never probed (per-call captcha), so recording the skip is
    # what keeps a strict sort from refusing forever.
    import json
    catalog = _catalog(tmp_path)
    reach = tmp_path / "reach.json"
    reach.write_text(json.dumps({
        "measured_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "reachable": ["workbuddy"],
        "unreachable": [],
        "skipped": {"zcode": "per-call captcha"},
    }))

    proc = _sort(catalog, reach, "--strict-coverage")

    assert proc.returncode == 0, proc.stderr


def test_strict_coverage_still_refuses_a_real_gap(tmp_path):
    import json
    catalog = _catalog(tmp_path)
    reach = tmp_path / "reach.json"
    reach.write_text(json.dumps({
        "measured_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "reachable": ["workbuddy"],
        "unreachable": [],
        "skipped": {},
    }))

    proc = _sort(catalog, reach, "--strict-coverage")

    assert proc.returncode == 4
    assert "zcode" in proc.stderr


def _aged_reach(tmp_path, hours, **over):
    """A reach snapshot whose verdicts are 'hours' old."""
    import json
    reach = tmp_path / "reach.json"
    stamp = (datetime.datetime.now(datetime.timezone.utc)
             - datetime.timedelta(hours=hours)).isoformat()
    snap = {
        "reachable": ["workbuddy"],
        "unreachable": ["cline"],
        "skipped": {},
        "measured_at": stamp,
        "verified_models": {},
    }
    snap.update(over)
    reach.write_text(json.dumps(snap), encoding="utf-8")
    return reach


def _order_fixture(tmp_path):
    """cline (early in --order) unreachable, qwen (late) unknown.

    The two only change places if the unreachable verdict stops being
    believed: both are then tier 1, and --order puts cline first.
    """
    import json
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"models": [
        {"slug": "qwen/qwen3.8-max"},
        {"slug": "cline/cline-free-deepseek-v4.1-flash"},
        {"slug": "workbuddy/glm-5.2"},
    ]}), encoding="utf-8")
    return catalog


def _order(catalog, reach, *extra):
    import json
    proc = _sort(catalog, reach, *extra)
    assert proc.returncode == 0, proc.stderr
    rows = json.loads(open(catalog, encoding="utf-8").read())["models"]
    return [m["slug"] for m in rows], proc


def test_a_stale_unreachable_verdict_stops_sinking(tmp_path):
    """An aged 'unreachable' is a claim about a moment, not a fact.

    Measured 2026-10-01: zcode/GLM-5.3-Flash answered a real chat completion,
    yet the picker still could not show it, because the reach snapshot was
    still calling providers dead from days earlier. The drop path already
    refuses to trust an aged verdict; ordering must not trust it either, or a
    provider that recovered stays buried below every unknown one forever.
    """
    catalog = _order_fixture(tmp_path)
    reach = _aged_reach(tmp_path, hours=96)

    order, proc = _order(catalog, reach)

    assert order == ["workbuddy/glm-5.2",
                     "cline/cline-free-deepseek-v4.1-flash",
                     "qwen/qwen3.8-max"]
    assert "not sinking stale" in proc.stderr
    # it is still not promoted to reachable: nothing here re-measured it
    assert order[0] == "workbuddy/glm-5.2"


def test_a_fresh_snapshot_still_sinks_an_unreachable_provider(tmp_path):
    """The control: while the verdict is current, nothing changes."""
    catalog = _order_fixture(tmp_path)
    reach = _aged_reach(tmp_path, hours=0.1)

    order, proc = _order(catalog, reach)

    assert order == ["workbuddy/glm-5.2", "qwen/qwen3.8-max",
                     "cline/cline-free-deepseek-v4.1-flash"]
    assert "not sinking stale" not in proc.stderr


def _rank_ordered(models):
    """Sort slugs the way main() does before it interleaves.

    interleave_reps() takes its input already sorted by (tier, provider
    position, lead rank, important rank); it does not sort on its own.
    """
    order = catalog_sort.DEFAULT_ORDER.split(",")

    def key(m):
        slug = m["slug"]
        prov = slug.split("/", 1)[0]
        return (order.index(prov) if prov in order else len(order),
                catalog_sort.important_rank(slug), slug)

    return sorted(models, key=key)


def test_interleave_reps_floats_each_providers_strongest_pair():
    """A provider runner-up belongs beside its strongest, not 60 rows down.

    Measured 2026-10-01 on the live catalog: zcode/GLM-5.3-Flash was the
    provider row 10 representative while zcode/GLM-5.3 sat at row 69 of
    117, because only one row per provider was floated.
    """
    models = [{"slug": s} for s in (
        "zcode/GLM-5.3", "zcode/GLM-5.3-Flash",
        "stepfun/step-5-preview", "stepfun/step-3.7-flash",
        "stepfun/step-3.5-flash",
        "qwen/qwen3.8-max")]

    slugs = [m["slug"] for m in catalog_sort.interleave_reps(
        _rank_ordered(models), catalog_sort.DEFAULT_ORDER.split(","),
        good={"zcode", "stepfun"})]

    flash = slugs.index("zcode/GLM-5.3-Flash")
    assert slugs.index("zcode/GLM-5.3") == flash + 1
    assert flash < 5
    # the third model of a provider is not floated: the band stays short
    assert (slugs.index("stepfun/step-3.5-flash")
            > slugs.index("stepfun/step-3.7-flash"))


def test_a_band_of_one_restores_the_single_representative(monkeypatch):
    """FLEET_REP_BAND=1 must bring back the old one-row-per-provider head."""
    monkeypatch.setattr(catalog_sort, "REP_BAND", 1)
    models = [{"slug": s} for s in (
        "zcode/GLM-5.3", "zcode/GLM-5.3-Flash",
        "stepfun/step-5-preview", "stepfun/step-3.7-flash")]

    slugs = [m["slug"] for m in catalog_sort.interleave_reps(
        _rank_ordered(models), catalog_sort.DEFAULT_ORDER.split(","),
        good={"zcode", "stepfun"})]

    # stepfun sits earlier in --order than zcode, so it still leads the band;
    # what changes is that zcode contributes a single row again.
    assert slugs.index("zcode/GLM-5.3-Flash") == 1
    assert slugs.index("zcode/GLM-5.3") == 3


def test_zcode_flash_ranks_before_the_bare_id():
    """GLM-5.3 is a substring of GLM-5.3-Flash.

    With the bare needle first both rows shared rank 0, so which one became
    the provider representative depended on the prober proven flag instead
    of on the listed order.
    """
    assert (catalog_sort.important_rank("zcode/GLM-5.3-Flash")
            < catalog_sort.important_rank("zcode/GLM-5.3"))


def test_rep_band_is_a_positive_int():
    assert isinstance(catalog_sort.REP_BAND, int)
    assert catalog_sort.REP_BAND >= 1

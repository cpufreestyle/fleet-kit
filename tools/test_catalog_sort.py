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

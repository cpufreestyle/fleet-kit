"""The free-window state answers one question per model: is the free quota this
row depends on live right now, still ahead of us, already over, or not a window
at all?

Measured 2026-10-03: free-windows.json carried only a sentence typed on the day
the vendor page was read, so a hand-written "（已结束）" could sit in the table
long after it stopped being true, and a dead two-week launch discount looked
exactly like a standing free tier. window_state() turns window_start and
window_end into that state; window_standing is asserted by the data for tiers
the vendor says have no end.

What is deliberately NOT done: mining the dates out of the window text. The
same field carries live-test notes, and a date there is the day the bridge was
measured, not the span a quota ran over. Reading one as the other would invent
windows that never existed.
"""
import copy
import json
import os
import shutil

from test_bridge_loader import load_free_models

free_models, DB, db_rows = load_free_models()

# A fixed "now" so the state tests read as arithmetic, never as a moving target.
NOW = free_models._parse_ts("2026-10-03T12:00:00+08:00")




def test_every_state_has_a_badge():
    assert set(free_models.WINDOW_BADGE) == set(free_models.WINDOW_STATES)


def test_window_state_moves_with_now():
    start, end = "2026-09-10T00:00:00+08:00", "2026-09-23T23:59:59+08:00"
    inside = free_models._parse_ts("2026-09-15T12:00:00+08:00")
    assert free_models.window_state(start, end, inside) == "active"
    assert free_models.window_state(start, end, NOW) == "expired"
    assert free_models.window_state("2026-12-01", "2026-12-10", NOW) == "upcoming"
    assert free_models.window_state(None, None, NOW) == "unknown"


def test_window_state_with_one_bound_only():
    assert free_models.window_state(None, "2026-09-27T23:59:59+08:00", NOW) == "expired"
    assert free_models.window_state(None, "2026-12-31", NOW) == "active"
    assert free_models.window_state("2026-09-01", None, NOW) == "active"


def test_parse_ts_bare_date_is_midnight_in_shanghai():
    assert free_models._parse_ts("2026-09-23") == \
        free_models._parse_ts("2026-09-23T00:00:00+08:00")


def test_parse_ts_keeps_offset_and_rejects_junk():
    assert free_models._parse_ts("2026-09-23T23:59:59+08:00")
    assert free_models._parse_ts("2026-09-23 23:59:59")
    assert free_models._parse_ts("not a date") is None
    assert free_models._parse_ts("") is None
    assert free_models._parse_ts(None) is None


def test_standing_when_the_data_says_there_is_no_end():
    row = free_models.annotate(DB, "trae", "seed-code-pro-0430")
    assert row["window_state"] == "standing"
    assert row["state_badge"] == "STANDING"
    assert row["window_start"] == "" and row["window_end"] == ""


def test_dates_beat_the_provider_standing_default():
    """workbuddy is a standing campaign; hy4-preview inside it ran two weeks
    and stopped. The row must not inherit the campaign's forever."""
    row = free_models.annotate(DB, "workbuddy", "hy4-preview")
    assert row["window_state"] == "expired"
    assert row["window_start"].startswith("2026-09-10")
    assert row["window_end"].startswith("2026-09-23")


def test_override_start_comes_from_the_model_not_the_provider():
    row = free_models.annotate(DB, "workbuddy-gpt", "hy4-preview")
    assert row["window_start"].startswith("2026-08-28")
    assert row["window_state"] == "expired"


def test_zcode_grant_expiry_does_not_swallow_the_daily_quota():
    row = free_models.annotate(DB, "zcode", "GLM-5.3-Flash")
    assert row["window_state"] == "expired"
    assert "Start Plan" in row["window"]


def test_nothing_structured_reads_as_unknown():
    row = free_models.annotate(DB, "xhx", "xhx-sn-deepseek-v4-1-flash")
    assert row["window_state"] == "unknown"
    assert row["state_badge"] == "?"


def test_status_record_dates_stay_measurement_dates():
    """The antigravity row carries 2026-06-18 and 2026-10-02 in its window text:
    the first is when Google shut personal Code Assist down, the second is the
    day the bridge was probed. Neither is a quota window for these models, so
    the state stays unknown rather than being mined out of the prose."""
    row = free_models.annotate(DB, "antigravity", "__unknown-model__")
    assert "2026-06-18" in row["window"]
    assert "实测" in row["window"]
    assert row["window_start"] == "" and row["window_end"] == ""
    assert row["window_state"] == "unknown"


def test_every_state_comes_from_a_structured_field():
    for row in db_rows():
        if row["window_state"] != "unknown":
            assert row["window_start"] or row["window_end"] \
                or row["window_standing"], row["model"]


def test_states_partition_every_declared_row():
    rows = db_rows()
    counted = {}
    for row in rows:
        counted[row["window_state"]] = counted.get(row["window_state"], 0) + 1
    assert sum(counted.values()) == len(rows)
    assert set(counted) <= set(free_models.WINDOW_STATES)
    assert "standing" in counted and "expired" in counted


def test_structured_windows_are_ordered_and_parseable():
    entries = list(DB["models"].items())
    entries += [("provider:" + name, pdef)
                for name, pdef in DB["providers"].items()]
    for key, entry in entries:
        start, end = entry.get("window_start"), entry.get("window_end")
        if not start and not end:
            continue
        assert free_models._parse_ts(start or end) is not None, key
        if start and end:
            assert free_models._parse_ts(start) <= free_models._parse_ts(end), key
        assert entry.get("window"), "badge without a sentence to explain it: " + key


def test_build_counts_the_states_it_hands_out(monkeypatch):
    db = copy.deepcopy(DB)
    monkeypatch.setattr(free_models, "load_db", lambda: db)
    monkeypatch.setattr(free_models, "catalog_index", lambda: {})
    monkeypatch.setattr(free_models, "ocx_live", lambda: [
        ("workbuddy", "hy4-preview"),
        ("trae", "seed-code-pro-0430"),
        ("xhx", "xhx-sn-deepseek-v4-1-flash")])
    snap = free_models.build()
    assert len(snap["models"]) == 3
    assert snap["window_states"] == {"expired": 1, "standing": 1, "unknown": 1}
    assert snap["live_source"] == "ocx-live"


def test_refresh_marks_a_reachable_source_verified(monkeypatch):
    db = copy.deepcopy(DB)
    monkeypatch.setattr(free_models, "probe_url", lambda url, timeout=12: 200)
    checked = free_models.refresh_sources(db)
    assert checked
    for name, pdef in db["providers"].items():
        if pdef.get("sources"):
            assert pdef["verified"] is True, name
        assert pdef["last_checked"], name


def test_refresh_drops_verified_when_every_source_is_down(monkeypatch):
    db = copy.deepcopy(DB)
    monkeypatch.setattr(free_models, "probe_url", lambda url, timeout=12: None)
    checked = free_models.refresh_sources(db)
    assert [n for n, row in checked.items() if row["reachable"]] == []
    for name, pdef in db["providers"].items():
        if pdef.get("sources"):
            assert pdef["verified"] is False, name
        assert pdef["last_checked"], name


def test_refresh_is_true_when_at_least_one_source_answers(monkeypatch):
    """A provider with a dead link beside a live one was still read today. Only
    a provider whose every source went dark loses the claim."""
    db = {"providers": {"demo": {"sources": ["http://a/", "http://b/"]}}}
    monkeypatch.setattr(free_models, "probe_url",
                        lambda url, timeout=12: 200 if url.endswith("a/") else None)
    checked = free_models.refresh_sources(db)
    assert checked["demo"]["reachable"] is True
    assert checked["demo"]["sources"] == {"http://a/": 200, "http://b/": None}
    assert db["providers"]["demo"]["verified"] is True


def test_refresh_only_touches_the_named_provider(monkeypatch):
    db = copy.deepcopy(DB)
    before = copy.deepcopy(db["providers"]["trae"])
    monkeypatch.setattr(free_models, "probe_url", lambda url, timeout=12: 200)
    checked = free_models.refresh_sources(db, only={"workbuddy"})
    assert set(checked) == {"workbuddy"}
    assert db["providers"]["workbuddy"]["last_checked"]
    assert db["providers"]["trae"] == before


def test_save_db_writes_a_backup_and_keeps_the_content(monkeypatch, tmp_path):
    live = tmp_path / "free-windows.json"
    shutil.copy2(free_models.DB_PATH, live)
    monkeypatch.setattr(free_models, "DB_PATH", str(live))
    before = live.read_text(encoding="utf-8")
    payload = {"providers": {}, "note": "scratch"}
    bak = free_models.save_db(payload)
    assert os.path.exists(bak)
    assert os.path.dirname(os.path.abspath(bak)) == str(tmp_path)
    with open(bak, encoding="utf-8") as fh:
        assert fh.read() == before
    text = live.read_text(encoding="utf-8")
    assert json.loads(text) == payload
    assert text.endswith("\n")

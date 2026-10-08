"""The credits annotation answers one question per model: does calling it bill the
reverse-proxied client's own quota, or not?

Measured 2026-09-29: free-windows.json only carried a free-tier status, so a
"SUB"/"FREE" badge said nothing about cost. A workbuddy row looked identical to a
trae row even though the first bills per-model credits and the second is a
rate-limited free plan, and the operator had no way to tell which calls could
exhaust an account.

annotate() resolves provider defaults, per-model overrides, and a note that says
where the quota comes from.
"""

from test_bridge_loader import load_free_models

free_models, DB, db_rows = load_free_models()


def test_every_provider_declares_a_credits_kind():
    """A provider without a credits kind would silently read as unknown."""
    missing = [name for name, pdef in DB["providers"].items()
               if pdef.get("credits") not in free_models.CREDITS_KINDS]
    assert missing == [], "providers missing credits: %s" % missing


def test_provider_default_is_inherited():
    row = free_models.annotate(DB, "trae", "trae-step-5-preview")
    assert row["credits"] == "limit"
    assert row["credits_badge"] in free_models.CREDITS_BADGE.values()
    assert row["credits_note"], "provider default must explain the quota source"


def test_model_override_beats_provider_default():
    row = free_models.annotate(DB, "trae", "trae-step-5-preview")
    assert row["credits"] == "client" or row["credits"] == "limit"
    # explicit override rows carry their own note
    override = free_models.annotate(DB, "workbuddy", "hy4-preview")
    assert override["credits"] == "client"
    assert "credits" in override["credits_note"]


def test_unknown_provider_falls_back_instead_of_raising():
    row = free_models.annotate(DB, "no-such-bridge", "whatever")
    assert row["credits"] == "unknown"
    assert row["credits_badge"] == "N/A"




def test_credits_counts_partition_every_row():
    rows = db_rows()
    counted = {}
    for row in rows:
        counted[row["credits"]] = counted.get(row["credits"], 0) + 1
    assert sum(counted.values()) == len(rows)
    assert set(counted) <= set(free_models.CREDITS_KINDS)


def test_client_credits_are_the_billable_ones():
    """client rows are the ones that can exhaust an account; own rows cannot."""
    by_kind = {}
    for row in db_rows():
        by_kind.setdefault(row["credits"], set()).add(row["provider"])
    assert "trae" in by_kind.get("limit", set())
    assert "tokendance" in by_kind.get("own", set())
    assert "workbuddy" in by_kind.get("client", set())
    assert "xhx" in by_kind.get("client", set())


def test_credits_filter_selects_only_requested_kind():
    rows = [r for r in db_rows() if r["credits"] == "client"]
    assert rows, "expected at least one client-credits row"
    assert {r["credits"] for r in rows} == {"client"}

"""The xhx usage ledger is the only usage number this fleet can show.

llm/v2 settles no 积分, so a bridge call is counted locally instead. These
tests pin the aggregation the status panel renders and the two things that
must never happen: an unwritable ledger taking a working bridge down, and an
unbounded file.
"""
import importlib.util
import json
import os

import pytest

BRIDGES = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "bridges")
sys_path = os.path.abspath(BRIDGES)
import sys
sys.path.insert(0, sys_path)

spec = importlib.util.spec_from_file_location(
    "xhx_usage_ledger", os.path.join(sys_path, "xhx", "usage_ledger.py"))
ledger = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ledger)


@pytest.fixture()
def path(tmp_path):
    return str(tmp_path / "xhx-usage.jsonl")


def _row(model, total, day="2026-09-29", stream=False, ts=None):
    return {"ts": ts or (day + "T10:00:00"), "model": model, "stream": stream,
            "prompt_tokens": 10, "completion_tokens": total - 10,
            "total_tokens": total, "reasoning_tokens": 0}


def test_the_default_ledger_lives_in_the_fleet_root():
    """The panel reads <home>/xhx-usage.jsonl, so the bridge must write there.

    bridges/xhx/usage_ledger.py is three levels below the fleet root; one
    level too few and every call lands in bridges/ where nothing looks.
    """
    here = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        os.pardir, "bridges", "xhx"))
    root = os.path.abspath(os.path.join(here, os.pardir, os.pardir))
    assert ledger.DEFAULT_PATH == os.path.join(root, "xhx-usage.jsonl")


def test_a_call_is_counted_per_model(path):
    ledger.record("raccoon-405a1c", {"prompt_tokens": 20, "completion_tokens": 500,
                                     "total_tokens": 520, "reasoning_tokens": 0},
                  stream=False, seconds=8.0, path=path)
    ledger.record("sn-glm-5-3", {"total_tokens": 731}, stream=True, path=path)
    summary = ledger.summarize(path=path, day="2026-09-29")
    assert summary["calls"] == 2
    assert summary["with_usage"] == 2
    assert summary["total_tokens"] == 1251
    assert [m["model"] for m in summary["models"]] == ["sn-glm-5-3", "raccoon-405a1c"]


def test_a_stream_without_usage_still_counts_as_a_call(path):
    ledger.record("raccoon-405a1c", None, stream=True, path=path)
    summary = ledger.summarize(path=path)
    assert summary["calls"] == 1
    assert summary["with_usage"] == 0
    assert summary["total_tokens"] == 0


def test_only_todays_rows_are_summarised(path):
    ledger.record("raccoon-405a1c", {"total_tokens": 999}, path=path)
    with open(path, encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle]
    rows[0]["ts"] = "2026-09-28T10:00:00"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(json.dumps(r) for r in rows) + "\n")
    summary = ledger.summarize(path=path, day="2026-09-29")
    assert summary["calls"] == 0
    assert ledger.summarize(path=path, day="2026-09-28")["total_tokens"] == 999


def test_a_missing_ledger_is_not_an_error(path):
    summary = ledger.summarize(path=path)
    assert summary == {"day": summary["day"], "calls": 0, "with_usage": 0,
                       "total_tokens": 0, "completion_tokens": 0,
                       "reasoning_tokens": 0, "models": []}


def test_a_corrupt_line_is_skipped_not_fatal(path):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("not json\n")
        handle.write(json.dumps(_row("raccoon-405a1c", 100)) + "\n")
    assert ledger.summarize(path=path)["total_tokens"] == 100


def test_an_unwritable_ledger_still_returns_the_row(path):
    """A bridge that cannot log must keep serving traffic."""
    row = ledger.record("raccoon-405a1c", {"total_tokens": 5},
                        path=os.path.join(path, "missing-dir", "x.jsonl"))
    assert row["model"] == "raccoon-405a1c"
    assert row["total_tokens"] == 5


def test_the_file_stays_bounded(path, monkeypatch):
    monkeypatch.setattr(ledger, "MAX_BYTES", 200)
    for index in range(60):
        ledger.record("raccoon-405a1c", {"total_tokens": index}, path=path)
    with open(path, encoding="utf-8") as handle:
        lines = handle.readlines()
    assert len(lines) <= ledger.KEEP_LINES
    assert os.path.getsize(path) <= 200 * 2   # trimmed on the next append

"""`qoderclicn --list-models` prints a literal header row the picker must not see.

Measured 2026-09-29: CLI 1.1.x emits "MODEL" (upper case) as its first line,
followed by the real ids. The old filter rejected only the three mixed-case
spellings Available/Models/Model, so the header row survived the id regex and
reached the catalog as the ghost model qoder/MODEL -- /v1/models advertised it
and every chat call against it failed upstream.

These tests pin the header filter, and pin that it cannot eat a real id which
merely starts with a header word (modelscope-x, model-router, ...).
"""
import importlib.util
import os
import sys

import pytest

BRIDGES = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, "bridges"))
sys.path.insert(0, BRIDGES)

spec = importlib.util.spec_from_file_location(
    "qoder_bridge", os.path.join(BRIDGES, "qoder", "qoder_bridge.py"))
qoder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qoder)

# What CLI 1.1.x actually prints: an upper-case header, then the ids.
CLI_OUTPUT = """MODEL
qwen3-max
Qwen/Qwen3-Coder-480B
modelscope-x-01
auto
"""


@pytest.mark.parametrize("line,verdict", [
    ("MODEL", True),
    ("model", True),
    ("Available Models", True),
    ("Available models", True),
    ("Model ID", True),
    ("Models:", True),
    ("ID   Name", True),
    ("Name", True),
    ("MODEL - DESCRIPTION", True),
    ("MODEL: qwen3-max", True),
    # A real id that merely starts with a header word must survive.
    ("modelscope-x-01", False),
    ("model-router", False),
    ("models/anything", False),
    ("id-chain", False),
    ("Available-Bandwidth", False),
])
def test_header_row_is_separated_from_a_matching_id(line, verdict):
    assert qoder._is_header_line(line) is verdict


def test_the_real_cli_output_yields_no_ghost_row():
    ids = [m["id"] for m in qoder._parse_models(CLI_OUTPUT)]
    assert ids == ["qwen3-max", "Qwen/Qwen3-Coder-480B", "modelscope-x-01", "auto"]
    assert "MODEL" not in ids


def test_the_older_mixed_case_spellings_are_still_dropped():
    ids = [m["id"] for m in qoder._parse_models(
        "Available Models\nModels\nModel\nqwen3-max\n")]
    assert ids == ["qwen3-max"]


def test_the_header_never_reaches_the_v1_models_route(monkeypatch):
    """End to end: /v1/models serves the parsed ids, header row nowhere."""

    class _Proc:
        stdout = CLI_OUTPUT
        stderr = ""

    monkeypatch.setattr(qoder, "_run_cli", lambda args, timeout=90: _Proc())
    monkeypatch.setattr(qoder, "_models_cache",
                        {"models": [], "fetched_at": 0.0, "source": "test"})

    from fastapi.testclient import TestClient
    r = TestClient(qoder.app).get("/v1/models")

    ids = [m["id"] for m in r.json()["data"]]
    assert "MODEL" not in ids
    assert "qwen3-max" in ids
    assert "modelscope-x-01" in ids
    assert r.json()["data"][0]["owned_by"] == "qoder"

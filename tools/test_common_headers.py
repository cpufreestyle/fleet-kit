"""A gateway diagnostic must never become a raw HTTP header value.

codely's /v1/models degrades to a static catalog when the gateway refuses,
attaching the upstream error body as a header. That body is JSON with
newlines, and h11 rejects those with LocalProtocolError mid-response, so
uvicorn tears the connection down and the client gets an empty reply -- worse
than no diagnostic at all. The route-level consequences live in
test_codely_recovery.py; these tests pin the sanitiser itself.
"""
import importlib.util
import os
import sys


BRIDGES = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, "bridges"))
sys.path.insert(0, BRIDGES)

spec = importlib.util.spec_from_file_location("_common", os.path.join(BRIDGES, "_common.py"))
_common = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_common)


def test_newlines_and_padding_collapse_into_one_space():
    raw = '{"error":{"message":"Authentication Error, Invalid proxy\nserver token.\nReceived API Key"}}'
    flat = _common.safe_header_value(raw)
    assert "\n" not in flat and "\r" not in flat
    assert flat == " ".join(raw.split())


def test_value_is_truncated_to_the_declared_bound():
    out = _common.safe_header_value("x" * 5000, max_chars=200)
    assert len(out) == 200


def test_non_string_input_is_coerced_not_crashed():
    assert _common.safe_header_value(None) == "None"
    assert _common.safe_header_value(12) == "12"

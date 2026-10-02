"""An upstream refusal wrapped in HTTP 200 must never read as a reply.

workbuddy's 11128 arrived as a 200 whose message text looked like an answer,
so every "did we get text?" checker called the bridge healthy while it could
never respond. These tests pin the shared detector and the two consumers that
judge reply text.
"""
import importlib.util
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

TOOLS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOLS)

import upstream_errors


REFUSALS = [
    '{"code":11128,"msg":"Illegal API invocation from an unapproved channel"}',
    "Request blocked. Please send it again",
    "请求被拦截，请重新发送",
    "請求已被攔截，請重新傳送",
    '{"error":{"message":"team not allowed to access model. This team can only '
    'access models=[\'alias-only-proxy-models\']. Tried to access DeepSeek-V4.1-Flash",'
    '"type":"team_model_access_denied"}}',
    "401 Authentication Error, Invalid proxy server token passed",
    "所有供应商已熔断，无可用渠道",
]

REPLIES = [
    "The quick brown fox jumps over the lazy dog.",
    "E2E_OK",
    "抱歉，我无法访问该链接。",
    "I cannot help with that request.",
    "",
    None,
]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, os.path.join(TOOLS, path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("body", REFUSALS)
def test_a_refusal_is_recognised(body):
    assert upstream_errors.is_error_body(body) is True


@pytest.mark.parametrize("body", REPLIES)
def test_a_real_reply_is_not_a_refusal(body):
    assert upstream_errors.is_error_body(body) is False


def test_the_marker_is_reported_so_the_note_names_the_cause():
    assert upstream_errors.matched_marker("Request blocked. Please send it again") == "request blocked"
    assert upstream_errors.matched_marker("all good") is None


def test_verify_real_calls_files_it_as_channel_blocked():
    """The verdict drives which rows the catalog filter may hide."""
    verify = _load("verify_real_calls_probe", "verify_real_calls.py")
    verdict, note = verify.classify(
        200, 3.0, REFUSALS[0], "A1B2", 12, 34, "")
    assert verdict == "CHANNEL_BLOCKED"
    assert "unapproved channel" in note


def test_a_real_answer_still_passes_the_arithmetic_gate():
    verify = _load("verify_real_calls_probe2", "verify_real_calls.py")
    verdict, note = verify.classify(200, 3.0, "暗号前四位 A1B2，12+34=46", "A1B2", 12, 34, "")
    assert verdict == "REAL"


class _Refusal(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 - http.server naming
        body = json.dumps({"choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": REFUSALS[0]}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def test_the_reachability_probe_rejects_the_refusal(tmp_path):
    """The probe decides catalog ordering: a refusal must not sort as reachable."""
    probe = _load("fleet_probe_probe", "fleet_probe.py")
    server = HTTPServer(("127.0.0.1", 0), _Refusal)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        ok, why = probe.try_call(server.server_port, {}, "whatever")
    finally:
        server.shutdown()
        server.server_close()
    assert ok is False
    assert "upstream refused" in why

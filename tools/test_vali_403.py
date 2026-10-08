"""The Google VALI gate is a browser check, not an outage and not a plan wall.

Measured 2026-10-02 on gemini (8794) and antigravity (8797). OAuth refresh on
both bridges still answers fine, and /health still reports the session alive,
but cloudcode-pa answers the first call with 403 "Verify your account to
continue." plus ErrorInfo{reason: VALI, metadata.validation_url}. The bridges
wrapped that in the usual 502 whose message was clipped to 300 chars, so the
operator saw a truncated JSON body ending mid-token in "r..." -- no link, and
nothing to act on.

These tests pin the three pieces of the fix: both bridges raise
AccountVerification (a UpstreamError whose str() keeps the link whole, so
_clip() cannot truncate it), and classify() grades the two shapes the operator
actually sees -- a bare 403 and the bridge's 502 envelope -- as
VERIFY_ACCOUNT. A VALI body says "permission denied", which the plan-block
word list would otherwise claim as PLAN_BLOCKED and send the operator to a
pricing page; VALI is checked first.
"""
import importlib.util
import json
import os
import sys
import threading
from http.server import ThreadingHTTPServer

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGES = os.path.abspath(os.path.join(HERE, os.pardir, 'bridges'))
if BRIDGES not in sys.path:
    sys.path.insert(0, BRIDGES)

from test_bridge_loader import load_bridge as _load




gemini = _load('vali_gemini_bridge', os.path.join('gemini', 'gemini_bridge.py'))
antigravity = _load('vali_antigravity_bridge',
                    os.path.join('antigravity', 'antigravity_bridge.py'))

_vspec = importlib.util.spec_from_file_location(
    'vali_verify_real_calls', os.path.join(HERE, 'verify_real_calls.py'))
vrc = importlib.util.module_from_spec(_vspec)
_vspec.loader.exec_module(vrc)


# The gate's real shape, with the single-use plt token replaced by a stable
# stand-in: the body is what Google answered, the token rotates every call.
VALI_URL = ('https://accounts.google.com/signin/continue?sarp=1&scc=1'
            '&continue=https://developers.google.com/gemini-code-assist'
            '/auth/auth_success_gemini&plt=AKgnsbvXMXZKu_HWSYZF-vC8LSQ_wHxr'
            'R5Iyw-JHF7gaFpv3RNms-x4S9HgXZPhb98Lt71_c5tu-Dt86cKi1f9y2MW7m'
            'Y0othQhS6iCXGLakebYvBUzMGlpjyUWwUX1sG4K4Ccq5ZQaX&flowName'
            '=GlifWebSignIn&authuser')
VALI_403 = json.dumps({
    'error': {
        'code': 403,
        'message': 'Verify your account to continue.',
        'status': 'PERMISSION_DENIED',
        'details': [{
            '@type': 'type.googleapis.com/google.rpc.ErrorInfo',
            'reason': 'VALI',
            'domain': 'googleapis.com',
            'metadata': {'validation_url': VALI_URL},
        }],
    }})

# What the bridges actually hand the operator: a 502 envelope whose message is
# the AccountVerification str() followed by the other channel's failure.
VALI_502_ENVELOPE = json.dumps({'error': {
    'message': 'code_assist: HTTP 403 VALIDATION_REQUIRED; account '
               'verification required, open: ' + VALI_URL
               + '; web: SNlM0e not found (web session expired?)',
    'type': 'upstream_error'}})


@pytest.mark.parametrize('bridge', [gemini, antigravity],
                         ids=['gemini', 'antigravity'])
class TestBothGoogleBridges:
    def test_vali_body_yields_the_validation_url(self, bridge):
        assert bridge.google_validation_url(VALI_403) == VALI_URL

    def test_a_plain_403_has_no_link(self, bridge):
        assert bridge.google_validation_url('{"error":{"code":403}}') is None
        assert bridge.google_validation_url('not json at all') is None

    def test_vali_error_str_keeps_the_link_whole(self, bridge):
        exc = bridge.AccountVerification(VALI_URL)
        assert VALI_URL in str(exc)
        assert exc.validation_url == VALI_URL

    def test_clip_does_not_truncate_a_validation_link(self, bridge):
        # The envelope clips every other message at 300 chars; a truncated
        # accounts.google.com/signin/continue/... URL is worthless.
        exc = bridge.AccountVerification(VALI_URL)
        assert len(str(exc)) > 300
        assert bridge._clip(exc) == str(exc)
        assert bridge._clip(ValueError('x' * 400)) == 'x' * 300

    def test_http_json_raises_account_verification(self, bridge):
        served = threading.Event()

        class Handler(bridge.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                served.set()
                self.send_response(403)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(VALI_403.encode())

            def log_message(self, *a):
                pass

        srv = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            with pytest.raises(bridge.AccountVerification) as ei:
                bridge.http_json('http://127.0.0.1:%d/x' % srv.server_port,
                                 {'x': 1})
            assert ei.value.validation_url == VALI_URL
        finally:
            srv.shutdown()
            srv.server_close()


class TestClassifier:
    def test_vali_403_grades_as_verify_account(self):
        # "permission denied" in the body would otherwise trip the plan
        # markers: the VALI check has to come first.
        v, note = vrc.classify(403, 0.2, '', 'ABCD', 1, 2, VALI_403)
        assert v == 'VERIFY_ACCOUNT'
        assert '验证账号' in note

    def test_vali_502_envelope_grades_as_verify_account(self):
        v, note = vrc.classify(502, 0.2, '', 'ABCD', 1, 2, VALI_502_ENVELOPE)
        assert v == 'VERIFY_ACCOUNT'

    def test_a_plain_permission_denied_403_still_reads_plan(self):
        body = '{"error":"permission denied for this model"}'
        v, _ = vrc.classify(403, 0.2, '', 'ABCD', 1, 2, body)
        assert v == 'PLAN_BLOCKED'

    def test_the_vali_markers_cover_both_wordings(self):
        """Google says it three ways; the marker list needs all of them."""
        for body in ('{"error":{"reason":"VALIDATION_REQUIRED"}}',
                     'Verify your account to continue.',
                     'account verification required, open: https://x'):
            v, _ = vrc.classify(403, 0.2, '', 'ABCD', 1, 2, body)
            assert v == 'VERIFY_ACCOUNT', body

    def test_a_missing_key_is_not_the_gate(self):
        body = json.dumps({'error': {'message': 'qwen bridge has no '
                                                'QWEN_API_KEY; set a real '
                                                'Qwen Cloud key in fleet.env '
                                                'and re-run bash '
                                                'bridges/finish.sh qwen',
                                    'type': 'qwen_key_missing'}})
        v, _ = vrc.classify(503, 0.0, '', 'ABCD', 1, 2, body)
        assert v == 'NO_KEY'

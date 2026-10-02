"""The zcode /health has to name the Z.AI account it is signed in as.

Measured 2026-10-01: the panel's account column drew a dash for zcode even
though the bridge held a valid JWT, because /health answered only
logged_in. The subject claim is the one identity the vendor issued -- the
coding-plan api-key carries none -- so /health now reads it out. These tests
pin that the claim reaches the panel, and that a payload without one answers
blank rather than a partial guess.
"""
import asyncio
import base64
import importlib.util
import json
import os
import sys
import unittest
from unittest import mock

BRIDGE_DIR = os.path.abspath(os.path.join(
    os.path.dirname(__file__), os.pardir, "bridges", "zcode"))
sys.path.insert(0, BRIDGE_DIR)

spec = importlib.util.spec_from_file_location(
    "zcode_bridge_health_account", os.path.join(BRIDGE_DIR, "zcode_bridge.py"))
zb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(zb)

SUB = "0a11fe3e-6eb1-425b-8cf7-64d574de8ec8"


def _jwt(claims):
    """An unsigned JWT carrying exactly the payload the test wants read back."""
    def seg(obj):
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    return "%s.%s.sig" % (seg({"alg": "none"}), seg(claims))


class TestTokenAccount(unittest.TestCase):
    """token_account: the account the JWT belongs to, or "" -- never a guess."""

    def test_the_subject_claim_names_the_account(self):
        with mock.patch.object(zb, "read_token",
                               return_value=_jwt({"sub": SUB})):
            self.assertEqual(zb.token_account(), SUB)

    def test_a_signed_out_bridge_names_no_account(self):
        # read_token already refuses a non-JWT (the coding-plan api-key), so
        # the signed-out path is the empty token it hands back.
        with mock.patch.object(zb, "read_token", return_value=""):
            self.assertEqual(zb.token_account(), "")

    def test_a_payload_with_no_identity_is_not_a_guess(self):
        with mock.patch.object(zb, "read_token",
                               return_value=_jwt({"exp": 1, "scope": "chat"})):
            self.assertEqual(zb.token_account(), "")

    def test_health_publishes_the_account(self):
        with mock.patch.object(zb, "read_token", return_value=_jwt(
                {"sub": SUB})), \
                mock.patch.object(zb, "read_captcha", return_value=""), \
                mock.patch.object(zb, "auth_state", return_value={}), \
                mock.patch.object(zb, "ordered_models",
                                  return_value=["glm-5.3"]):
            health = asyncio.run(zb.health())
        self.assertTrue(health["ok"])
        self.assertTrue(health["logged_in"])
        self.assertEqual(health["account"], SUB)

    def test_health_keeps_the_account_blank_when_signed_out(self):
        with mock.patch.object(zb, "read_token", return_value=""), \
                mock.patch.object(zb, "read_captcha", return_value=""), \
                mock.patch.object(zb, "auth_state", return_value={}), \
                mock.patch.object(zb, "ordered_models",
                                  return_value=["glm-5.3"]):
            health = asyncio.run(zb.health())
        self.assertFalse(health["logged_in"])
        self.assertEqual(health["account"], "")

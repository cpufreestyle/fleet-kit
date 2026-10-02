"""CatPaw must remember which upstream answered, instead of re-walking all three.

Measured 2026-09-29 on the live bridge: get_token() cost 7.81s on a cold
process. It validates the credential against BASE, MCOPILOT and PUBLIC_BASE in
that fixed order, and two of the three are unreachable from this network -- each
failure costs ~2.6s waiting for the proxy tunnel to give up. /health calls
get_token() on every cold start, so a cold /health answered 200 after 10.9s.

Remembering the base that last answered does not change the validation or the
fallback chain -- a base that used to answer and stopped is still retried, and
the other two are still walked when the remembered one fails -- it only changes
the order they are tried in, so the common case stops paying for bases that are
known to be dead.
"""
import importlib.util
import os
import sys
import unittest
from unittest import mock

BRIDGE_DIR = os.path.abspath(os.path.join(
    os.path.dirname(__file__), os.pardir, "bridges", "catpaw"))
sys.path.insert(0, BRIDGE_DIR)

spec = importlib.util.spec_from_file_location(
    "catpaw_token_order", os.path.join(BRIDGE_DIR, "catpaw_bridge.py"))
cp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cp)


def _host(url):
    return url.split("//")[1].split("/")[0]


def _cold():
    cp.ST["at"] = None
    cp.ST["ts"] = 0.0
    cp._TOKEN_BASE["base"] = ""
    cp._REACH["data"] = {}


class CatpawTokenBaseOrderTest(unittest.TestCase):
    def setUp(self):
        _cold()

    def test_bases_in_order_puts_the_remembered_one_first_and_keeps_all_three(self):
        cp._TOKEN_BASE["base"] = cp.MCOPILOT
        order = cp.bases_in_order()
        self.assertEqual(order[0], cp.MCOPILOT)
        self.assertEqual(set(order), {cp.BASE, cp.MCOPILOT, cp.PUBLIC_BASE})
        self.assertEqual(len(order), 3, "a base must never appear twice")

    def test_bases_in_order_without_a_memory_is_the_documented_order(self):
        self.assertEqual(cp.bases_in_order(),
                         [cp.BASE, cp.MCOPILOT, cp.PUBLIC_BASE])

    def _walk(self, only_public_answers):
        """Run one cold get_token and report the hosts it hit, in order."""
        seen = []

        def fake(url, data=None, headers=None, method="GET", timeout=60, stream=False):
            seen.append(url)
            live = cp.PUBLIC_BASE if only_public_answers else cp.MCOPILOT
            if url.startswith(live):
                return 200, '{"mis": "13661621468"}', {}
            return 0, "URLError: tunnel failed", {}

        with mock.patch.object(cp, "read_state", lambda: {"at_idekit": "tok-1"}), \
                mock.patch.object(cp, "http_req", fake):
            token = cp.get_token(force=True)
        return token, [_host(u) for u in seen]

    def test_the_base_that_answered_is_tried_alone_next_time(self):
        # cold: the documented order, so two dead bases before the live one
        first, hosts_cold = self._walk(only_public_answers=True)
        self.assertEqual(first, "tok-1")
        self.assertEqual(hosts_cold, ["catpaw.sankuai.com",
                                         "mcopilot-emb.sankuai.com",
                                         "catpaw.meituan.com"])
        self.assertEqual(cp._TOKEN_BASE["base"], cp.PUBLIC_BASE)

        # warm: only the remembered base, no dead ones on the way
        cp.ST["at"], cp.ST["ts"] = None, 0.0
        second, hosts_warm = self._walk(only_public_answers=True)
        self.assertEqual(second, "tok-1")
        self.assertEqual(hosts_warm, ["catpaw.meituan.com"])

    def test_a_host_that_reachability_just_answered_is_tried_first(self):
        """The /health probe runs before get_token and its verdict
        survives into the walk, so a freshly restarted bridge does not pay
        for the two hosts the network cannot reach at all.
        """
        cp._REACH["data"] = {
            "catpaw.sankuai.com": [0, "unreachable"],
            "mcopilot-emb.sankuai.com": [0, "unreachable"],
            "catpaw.meituan.com": [200, "ok"],
        }
        order = cp.bases_in_order()
        self.assertEqual(order[0], "https://catpaw.meituan.com")
        self.assertEqual(set(order), {cp.BASE, cp.MCOPILOT, cp.PUBLIC_BASE})
        self.assertEqual(len(order), 3, "still every base, once")

    def test_a_different_answerer_is_still_found(self):
        """The fallback chain must survive: only MCOPILOT answers here."""
        token, hosts = self._walk(only_public_answers=False)
        self.assertEqual(token, "tok-1")
        # no memory yet, so the documented order stands: BASE first, then the
        # base that answers. The chain is not skipped, only reordered.
        self.assertEqual(hosts, ["catpaw.sankuai.com", "mcopilot-emb.sankuai.com"])
        self.assertEqual(cp._TOKEN_BASE["base"], cp.MCOPILOT)


if __name__ == "__main__":
    unittest.main()

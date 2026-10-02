"""CatPaw /health must not cost ten seconds to answer.

Measured 2026-09-29 on the live bridge (pid 2368, code from 17:20):

    curl -m 6  /health  ->  HTTP 000 after 6.0s   (looked dead)
    curl -m 30 /health  ->  HTTP 200 after 10.1s

The handler walked BASE, MCOPILOT and PUBLIC_BASE one after another with a
5s timeout each, and two of the three are unreachable from this network --
each failure costs ~2.6s waiting for the proxy tunnel to give up, so three
bases cost ~10s. get_token() then validated the credential against the same
three bases for a further ~7.8s.

Scope, stated precisely because it is narrower than it first looks: the
fleet's probers do NOT call /health. verify_real_calls.py, fleet_probe.py and
status_ui.py all probe /v1/models, which for catpaw is the cached
list_models() and answers in microseconds, so no bridge was ever misreported
by them. What the cost lands on is the operator -- a manual curl of /health
looks exactly like a hung bridge -- plus one slow request right after a
restart, because get_token() caches its validation for 1500s.

Reachability is a diagnostic, so it is bounded two ways here: the three pings
run concurrently, making the cost the slowest single ping instead of their
sum, and a complete result is cached for REACH_TTL seconds so a dashboard that
polls every few seconds does not pay again. Measured after the fix on the
restarted bridge: 3.01s cold, 0.0007s on every call after.
"""
import importlib.util
import os
import sys
import threading
import time
import unittest

BRIDGE_DIR = os.path.abspath(os.path.join(
    os.path.dirname(__file__), os.pardir, "bridges", "catpaw"))
sys.path.insert(0, BRIDGE_DIR)

spec = importlib.util.spec_from_file_location(
    "catpaw_health_latency", os.path.join(BRIDGE_DIR, "catpaw_bridge.py"))
cp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cp)


class _ConcurrentProbe:
    """Fake http_req that records how many pings were in flight at once."""

    def __init__(self, delay=0.2):
        self.delay = delay
        self.inflight = 0
        self.max_inflight = 0
        self.calls = []
        self._lock = threading.Lock()

    def __call__(self, url, data=None, headers=None, method="GET",
                 timeout=60, stream=False):
        with self._lock:
            self.calls.append(url)
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
        time.sleep(self.delay)
        with self._lock:
            self.inflight -= 1
        return 0, "unreachable", {}


class CatpawHealthLatencyTest(unittest.TestCase):
    def setUp(self):
        # reachability is module-level cached state; start every test cold
        cp._REACH["at"] = 0.0
        cp._REACH["data"] = {}

    def test_reachability_pings_run_concurrently(self):
        probe = _ConcurrentProbe(delay=0.2)
        original = cp.http_req
        cp.http_req = probe
        try:
            started = time.monotonic()
            reach = cp.probe_reach()
            elapsed = time.monotonic() - started
        finally:
            cp.http_req = original

        self.assertEqual(len(probe.calls), 3)
        self.assertGreater(probe.max_inflight, 1,
                             "the three pings must overlap, not run in series")
        # serial would be ~3 x 0.2s; concurrent is ~0.2s
        self.assertLess(elapsed, 0.5, "series pings cost 3x the slowest one")
        self.assertEqual(set(reach), {"catpaw.sankuai.com",
                                         "mcopilot-emb.sankuai.com",
                                         "catpaw.meituan.com"})

    def test_reachability_is_cached_for_the_ttl(self):
        probe = _ConcurrentProbe(delay=0.05)
        original = cp.http_req
        cp.http_req = probe
        try:
            cp.probe_reach()
            calls_after_first = len(probe.calls)
            for _ in range(4):
                cp.probe_reach()
        finally:
            cp.http_req = original

        self.assertEqual(calls_after_first, 3)
        self.assertEqual(len(probe.calls), 3,
                             "a dashboard polling every second must not re-ping")

    def test_reachability_shape_is_host_to_status_and_label(self):
        def fake(url, data=None, headers=None, method="GET", timeout=60, stream=False):
            if url.startswith(cp.PUBLIC_BASE):
                return 200, "ok", {}
            return 0, "URLError: timed out", {}

        original = cp.http_req
        cp.http_req = fake
        try:
            reach = cp.probe_reach()
        finally:
            cp.http_req = original

        self.assertEqual(reach["catpaw.meituan.com"], [200, "ok"])
        self.assertEqual(reach["catpaw.sankuai.com"][1], "unreachable")


if __name__ == "__main__":
    unittest.main()

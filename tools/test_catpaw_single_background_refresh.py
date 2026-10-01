"""A burst of /v1/models polls must start one background refresh, not one each.

list_models() is the endpoint every prober calls: status_ui polls it,
fleet_probe runs it every few minutes, verify_real_calls on demand. Its cached
path used to spawn a _bg_refresh thread on every single call, and _bg_refresh
calls get_token(), which takes ST['lock'] and walks upstream hosts. So a burst
of polls left a pile of threads serialized on that lock, and /health -- which
also needs get_token() -- queued behind them. Measured 2026-09-29: after three
/v1/models calls, /health took 10.7s while its own components measured 3.7s.

The refresh is idempotent and its result is cached, so one in flight is enough.
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
    "catpaw_single_bg", os.path.join(BRIDGE_DIR, "catpaw_bridge.py"))
cp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cp)


class CatpawSingleBackgroundRefreshTest(unittest.TestCase):
    def setUp(self):
        cp.ST["models"] = [{"modelTypeName": "glm-5.3"}]
        cp.ST["models_ts"] = 0.0
        cp._BG_RUNNING["on"] = False

    def _install(self, started, release):
        original = cp._bg_refresh

        def fake_refresh():
            started.append(1)
            release.wait(10)

        cp._bg_refresh = fake_refresh
        return original

    def _teardown(self, release, original):
        """Release the refresh thread and wait for the guard to drop.

        _spawn_bg_refresh() raises the guard before Thread.start() and the
        thread clears it only once fake_refresh() has returned, so setting the
        event is not the end of it: the thread still has to be scheduled. A
        test that returns inside that window leaves its thread holding the
        guard, and that thread then clears the guard for the next test
        mid-burst -- which reads as one-at-a-time having become twice at a
        time. Measured 2026-09-30 under the full suite (1 failed, 328 passed):
        test_the_guard_reopens_once_the_refresh_finishes saw started == 2 here,
        and the same file passes alone every time.
        """
        release.set()
        for _ in range(500):
            if not cp._BG_RUNNING["on"]:
                break
            time.sleep(0.02)
        cp._bg_refresh = original

    def test_a_burst_of_polls_starts_one_refresh(self):
        started, release = [], threading.Event()
        original = self._install(started, release)
        try:
            for _ in range(10):
                cp.list_models()
            self.assertEqual(len(started), 1,
                             "ten polls must not start ten refreshes")
        finally:
            self._teardown(release, original)

    def test_the_guard_reopens_once_the_refresh_finishes(self):
        """One-at-a-time must not become once-ever."""
        started, release = [], threading.Event()
        original = self._install(started, release)
        try:
            cp.list_models()
            cp.list_models()
            self.assertEqual(len(started), 1)
            release.set()
            for _ in range(200):
                if not cp._BG_RUNNING["on"]:
                    break
                time.sleep(0.02)
            cp.list_models()
            self.assertEqual(len(started), 2,
                             "a later poll must still be able to refresh")
        finally:
            self._teardown(release, original)


if __name__ == "__main__":
    unittest.main()

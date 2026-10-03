"""A slow collect() round must never park /api/status on "loading".

Measured 2026-10-02: collect_cached() held _COLLECT_LOCK for the whole round.
free_models() (subprocess timeout 60s) and node_credits() (120s) stall for
minutes when the international links are down, and every /api/status poll that
arrived after COLLECT_TTL_SECONDS queued behind that round -- so the dashboard
stayed on "loading" for minutes at a time (curl -m 100 never returned).

Now an expired cache is served immediately (stale=True) while one daemon
worker re-collects; only a cold start with no data at all waits, and a failing
round degrades instead of wedging the panel.
"""
import importlib.util
import os
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "status_ui", os.path.join(HERE, "status_ui.py"))
status_ui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(status_ui)

CFG = {"home": "/tmp"}


class StaleWhileRevalidateTest(unittest.TestCase):
    def setUp(self):
        status_ui.COLLECT_TTL_SECONDS = 0.05
        status_ui.COLLECT_COLD_TIMEOUT = 5.0
        status_ui._COLLECT_CACHE.update(
            {"at": 0.0, "value": None, "refreshing": False, "worker": None})
        self._orig_refresh = status_ui.refresh_keys
        self._orig_collect = status_ui.collect
        self.gate = threading.Event()
        self.collect_calls = []

        def fake_collect(cfg):
            self.collect_calls.append(1)
            self.gate.wait(10)
            return {"generated_at": "now", "round": len(self.collect_calls)}

        status_ui.refresh_keys = lambda cfg: None
        status_ui.collect = fake_collect

    def tearDown(self):
        self.gate.set()
        worker = status_ui._COLLECT_CACHE["worker"]
        if worker is not None:
            worker.join(5)
        status_ui.refresh_keys = self._orig_refresh
        status_ui.collect = self._orig_collect
        status_ui._COLLECT_CACHE.update(
            {"at": 0.0, "value": None, "refreshing": False, "worker": None})

    def _wait_for_fresh(self, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with status_ui._COLLECT_LOCK:
                value = status_ui._COLLECT_CACHE["value"]
                if value is not None and value.get("stale") is False:
                    return value
            time.sleep(0.01)
        raise AssertionError("worker never stored a fresh value")

    def test_cold_start_waits_for_the_first_round(self):
        threading.Timer(0.3, self.gate.set).start()
        started = time.time()
        value = status_ui.collect_cached(CFG)
        self.assertFalse(value.get("stale"))
        self.assertEqual(value["round"], 1)
        self.assertGreaterEqual(time.time() - started, 0.25)

    def test_expired_cache_serves_stale_without_waiting(self):
        with status_ui._COLLECT_LOCK:
            status_ui._COLLECT_CACHE.update(
                {"at": time.time() - 100.0, "value": {"round": 1},
                 "refreshing": False, "worker": None})
        started = time.time()
        value = status_ui.collect_cached(CFG)
        elapsed = time.time() - started
        self.assertTrue(value.get("stale"))
        self.assertEqual(value["round"], 1)
        self.assertLess(elapsed, 0.15)
        self.gate.set()
        fresh = self._wait_for_fresh()
        self.assertFalse(fresh.get("stale"))

    def test_a_burst_of_polls_starts_one_refresh(self):
        with status_ui._COLLECT_LOCK:
            status_ui._COLLECT_CACHE.update(
                {"at": time.time() - 100.0, "value": {"round": 1},
                 "refreshing": False, "worker": None})
        # let the first poll start the worker, then hammer the endpoint while
        # the round is still in flight (gate not set yet)
        first = status_ui.collect_cached(CFG)
        for _ in range(200):
            if self.collect_calls:
                break
            time.sleep(0.01)
        for _ in range(5):
            status_ui.collect_cached(CFG)
        self.assertTrue(first.get("stale"))
        self.assertEqual(len(self.collect_calls), 1)

    def test_a_failing_round_degrades_and_reopens(self):
        with status_ui._COLLECT_LOCK:
            status_ui._COLLECT_CACHE.update(
                {"at": time.time() - 100.0, "value": {"round": 1},
                 "refreshing": False, "worker": None})

        def boom(cfg):
            raise RuntimeError("tunnel connection failed")

        status_ui.collect = boom
        status_ui.collect_cached(CFG)
        self._wait_for_attempt()
        worker = status_ui._COLLECT_CACHE["worker"]
        if worker is not None:
            worker.join(5)
        with status_ui._COLLECT_LOCK:
            stored = status_ui._COLLECT_CACHE["value"]
            self.assertFalse(status_ui._COLLECT_CACHE["refreshing"])
        self.assertTrue(any("采集失败" in w for w in stored["warnings"]))
        self.assertTrue(stored.get("error"))

        # the panel keeps serving after a failed round
        self.gate.set()

        def fake_collect(cfg):
            return {"generated_at": "now", "round": 7}

        status_ui.collect = fake_collect
        status_ui._COLLECT_CACHE["at"] = 0.0
        value = status_ui.collect_cached(CFG)
        worker = status_ui._COLLECT_CACHE["worker"]
        if worker is not None:
            worker.join(5)
        with status_ui._COLLECT_LOCK:
            stored = status_ui._COLLECT_CACHE["value"]
        self.assertEqual(stored["round"], 7)

    def _wait_for_attempt(self, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with status_ui._COLLECT_LOCK:
                if not status_ui._COLLECT_CACHE["refreshing"]:
                    return
            time.sleep(0.01)
        raise AssertionError("worker never finished the failing round")


if __name__ == "__main__":
    unittest.main()

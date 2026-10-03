"""The generic key pool must keep a node alive when one key stops working.

xhx proved the shape with session files (bridges/xhx/account_pool.py) and
workbuddy proved it before that. What plan_key_pool.py keeps from that port is
the part that matters and drops is the session machinery, because Kimi Code and
MiniMax bill a plain API key. Pinned here:

* one key per 0600 account file under auths/, and nothing written anywhere else
* candidates() order: primary, then the account that last answered, then the
  least recently used -- so a pool with three keys never asks the same one twice
* a cooled key is skipped and comes back once its cooldown lapses
* request_with_pool() fails over on a verdict, and passes a verdict-less reply
  straight through: a 400 is the provider's answer to the *request*, not a
  broken key, and cooling for it would take a working key out of service
* refresh_points() reads through an injected reader, so no vendor URL is
  hard-coded in the pool
"""

import asyncio
import importlib.util
import sys
import tempfile
import time
import unittest
from pathlib import Path

import httpx

BRIDGES = Path(__file__).resolve().parent.parent / "bridges"
sys.path.insert(0, str(BRIDGES))

_spec = importlib.util.spec_from_file_location("plan_key_pool", BRIDGES / "plan_key_pool.py")
plan_key_pool = importlib.util.module_from_spec(_spec)
# @dataclass needs the module in sys.modules to resolve its annotations
sys.modules["plan_key_pool"] = plan_key_pool
_spec.loader.exec_module(plan_key_pool)


class FakeResponse:
    """The two methods request_with_pool() needs from an httpx response."""

    def __init__(self, status, body="{}"):
        self.status_code = status
        self._body = body.encode("utf-8")
        self.closed = False

    async def aread(self):
        return self._body

    async def aclose(self):
        self.closed = True


def verdict_401_dead(status, body):
    """A key the upstream refuses is the only verdict that must cool a key."""
    if status == 401:
        return "upstream refused the key", 3600, "key_dead"
    return None


class KeyPoolTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def pool(self, seed=None, reader=None):
        return plan_key_pool.KeyPool(
            "kimi", self.root / "auths",
            seed or (lambda: [("sk-first", "env KIMI_CODING_API_KEY")]),
            read_points=reader, points_ttl=90)

    # ---------------- storage ----------------

    def test_one_key_one_file_private_to_the_user(self):
        pool = self.pool()
        accounts = pool.status()
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0]["source"], "env KIMI_CODING_API_KEY")
        self.assertTrue(accounts[0]["primary"])
        files = list((self.root / "auths").glob("kimi-*.json"))
        self.assertEqual(len(files), 1)
        self.assertEqual(oct(files[0].stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct((self.root / "auths").stat().st_mode & 0o777), "0o700")
        # the key is on disk in full and masked in every view
        self.assertIn("sk-first", files[0].read_text(encoding="utf-8"))
        self.assertNotIn("sk-first", [item["key_tail"] for item in accounts])
        # seeding writes nothing outside its own directory
        self.assertEqual([item.name for item in self.root.iterdir()], ["auths"])

    def test_the_pool_survives_a_restart(self):
        pool = self.pool()
        pool.add("sk-second", source="admin")
        pool.set_primary(pool.add("sk-third", source="admin")["ref"])
        first_ref = pool.status()[0]["ref"]

        again = plan_key_pool.KeyPool("kimi", self.root / "auths", lambda: [])
        state = {item["name"]: item for item in again.status()}
        self.assertEqual(len(state), 3)
        self.assertTrue(again.status()[0]["primary"])
        self.assertTrue(again.status()[0]["source"])
        # a seed that offers nothing must not duplicate what is already there
        self.assertEqual(len(list((self.root / "auths").glob("kimi-*.json"))), 3)
        self.assertIn(first_ref, {item["ref"] for item in again.status()})

    def test_the_pool_never_reaches_outside_its_own_directory(self):
        pool = self.pool()
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        # a key dropped in a directory that is not the pool's is not an account
        (elsewhere / "kimi-deadbeefdeadbeef.json").write_text(
            '{"key": "sk-sneaky"}', encoding="utf-8")
        pool.reload()
        self.assertEqual([item["key_tail"] for item in pool.status()], ["irst"])

        # and an account whose file was moved out from under the pool is not
        # deleted: remove() refuses a path it does not own
        added = pool.add("sk-second", source="admin")
        object.__setattr__(pool._accounts[added["ref"]], "path", elsewhere / "moved.json")
        with self.assertRaises(RuntimeError):
            pool.remove(added["ref"])
        # refused means nothing was unlinked: both files are still there
        self.assertEqual(len(list((self.root / "auths").glob("kimi-*.json"))), 2)
        self.assertFalse((elsewhere / "moved.json").exists())

    # ---------------- selection ----------------

    def test_candidates_prefer_primary_then_active_then_least_recently_used(self):
        pool = self.pool()
        pool.add("sk-second", source="admin")
        pool.add("sk-third", source="admin")
        first, second, third = [item["ref"] for item in pool.status()]

        # nobody has answered yet: primary first, then least recently used
        self.assertEqual([item.ref for item in pool.candidates()],
                         [first, second, third])
        pool.mark_success(third)
        self.assertEqual([item.ref for item in pool.candidates()],
                         [first, third, second])
        pool.mark_failure(first, "key refused", 3600)
        self.assertEqual([item.ref for item in pool.candidates()], [third, second])

    def test_a_cooled_key_is_skipped_until_its_cooldown_lapses(self):
        pool = self.pool()
        pool.add("sk-second", source="admin")
        first, second = [item["ref"] for item in pool.status()]
        pool.mark_failure(first, "rate limited", 60)

        self.assertEqual([item.ref for item in pool.candidates()], [second])
        states = {item["ref"]: item["state"] for item in pool.status()}
        self.assertEqual(states[first], "cooling")
        self.assertIn("rate limited", [item["reason"] for item in pool.status()])
        with pool._lock:
            pool._state["accounts"][first]["cooldown_until"] = time.time() - 1
        self.assertEqual(len(pool.candidates()), 2)

    # ---------------- request loop ----------------

    def test_a_refused_key_fails_over_to_the_next_one(self):
        pool = self.pool()
        pool.add("sk-second", source="admin")
        first, second = [item["ref"] for item in pool.status()]
        seen = []

        async def send(key):
            seen.append(key)
            status = 401 if key == "sk-first" else 200
            return FakeResponse(status, "{}" if status == 200 else '{"error":"nope"}')

        candidate, response = asyncio.run(
            plan_key_pool.request_with_pool(pool, send, verdict_401_dead))

        self.assertEqual(seen, ["sk-first", "sk-second"])
        self.assertEqual(candidate.ref, second)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(pool._state["active_ref"], second)
        states = {item["ref"]: item for item in pool.status()}
        self.assertEqual(states[first]["state"], "cooling")
        self.assertIn("refused", states[first]["reason"])

    def test_a_request_level_failure_is_not_a_key_failure(self):
        pool = self.pool()
        pool.add("sk-second", source="admin")

        async def send(key):
            return FakeResponse(400, '{"error":{"message":"unknown model"}}')

        candidate, response = asyncio.run(
            plan_key_pool.request_with_pool(pool, send, verdict_401_dead))

        # classify() said nothing, so the provider's answer goes back untouched
        # and no account was cooled for it
        self.assertEqual(response.status_code, 400)
        self.assertTrue(candidate.ref)
        self.assertEqual([item.state for item in pool.candidates()]
                         if hasattr(pool.candidates()[0], "state") else
                         [item["state"] for item in pool.status()],
                         ["ready", "ready"])

    def test_every_key_dead_raises_exhausted_with_each_failure(self):
        pool = self.pool()
        pool.add("sk-second", source="admin")

        async def send(key):
            return FakeResponse(401, '{"error":{"message":"invalid key"}}')

        with self.assertRaises(plan_key_pool.PoolExhausted) as ctx:
            asyncio.run(plan_key_pool.request_with_pool(pool, send, verdict_401_dead))
        self.assertEqual(len(ctx.exception.failures), 2)
        self.assertEqual([item[4] for item in ctx.exception.failures],
                         ["key_dead", "key_dead"])
        self.assertEqual(ctx.exception.status, 401)
        # both keys are cooling, which is a different error from "no keys"
        self.assertEqual(pool.candidates(), [])

    def test_a_transport_error_cools_briefly_and_reports_its_type(self):
        pool = self.pool()

        async def send(key):
            # httpx is what the bridges send with, and its own exception type
            # is the name that says "the socket never opened"
            raise httpx.ConnectError("api.kimi.com refused")

        with self.assertRaises(plan_key_pool.PoolExhausted) as ctx:
            asyncio.run(plan_key_pool.request_with_pool(pool, send, verdict_401_dead))
        self.assertEqual(ctx.exception.failures[0][4], "transport")
        self.assertEqual(ctx.exception.failures[0][2], "api.kimi.com refused")
        # the exception type is what names the broken layer, and the pool keeps it
        reason = pool.status()[0]["reason"]
        self.assertIn("ConnectError", reason)
        self.assertIn("api.kimi.com refused", reason)

    def test_an_empty_pool_raises_unavailable_without_any_call(self):
        pool = self.pool(seed=lambda: [])
        called = []

        async def send(key):
            called.append(key)
            return FakeResponse(200)

        with self.assertRaises(plan_key_pool.PoolUnavailable):
            asyncio.run(plan_key_pool.request_with_pool(pool, send, verdict_401_dead))
        self.assertEqual(called, [])
        self.assertEqual(pool.status(), [])

    # ---------------- points ----------------

    def test_points_come_back_through_the_injected_reader(self):
        seen = []

        def reader(key):
            seen.append(key)
            return {"points": 4200, "unit": "credits", "plan": "Kimi Code",
                    "detail": "remains=4200", "error": ""}

        pool = self.pool(reader=reader)
        summary = asyncio.run(pool.refresh_points(force=True))
        item = summary["accounts"][0]
        self.assertEqual(seen, ["sk-first"])
        self.assertEqual(item["points"], 4200)
        self.assertEqual(item["points_unit"], "credits")
        self.assertTrue(item["points_updated_at"])
        # the TTL decides, so a second refresh does not re-read
        self.assertTrue(pool.points_stale() is False)
        asyncio.run(pool.refresh_points())
        self.assertEqual(seen, ["sk-first"])

    def test_a_reader_that_cannot_answer_records_why_not(self):
        def reader(key):
            return {"points": None, "unit": "", "plan": "",
                    "detail": "GET /remains -> HTTP 401", "error": "401: needs a plan"}

        pool = self.pool(reader=reader)
        summary = asyncio.run(pool.refresh_points(force=True))
        item = summary["accounts"][0]
        self.assertIsNone(item["points"])
        self.assertEqual(item["points_error"], "401: needs a plan")

    def test_a_reader_that_blows_up_does_not_take_the_pool_down(self):
        def reader(key):
            raise OSError("no route to host")

        pool = self.pool(reader=reader)
        summary = asyncio.run(pool.refresh_points(force=True))
        item = summary["accounts"][0]
        self.assertIsNone(item["points"])
        self.assertIn("OSError", item["points_error"])
        self.assertEqual(len(pool.candidates()), 1)


class NumberReadingTest(unittest.TestCase):
    """The readers only know the shape once a live key answers."""

    def test_a_remaining_counter_wins_over_a_used_one(self):
        numbers = plan_key_pool.walk_numbers(
            {"used": 812, "windows": [{"name": "daily", "remain": 4200}]})
        value, unit = plan_key_pool.headline_number(numbers)
        self.assertEqual(value, 4200)
        self.assertEqual(unit, "remain")

    def test_an_unknown_shape_still_yields_something(self):
        value, unit = plan_key_pool.headline_number(
            plan_key_pool.walk_numbers({"total_quota": 99}))
        self.assertEqual(value, 99)
        self.assertEqual(unit, "total_quota")

    def test_booleans_are_not_balances(self):
        self.assertEqual(plan_key_pool.walk_numbers({"enabled": True, "left": 5}),
                         [("left", 5)])

    def test_a_name_tells_two_keys_apart_without_logging_them(self):
        name = plan_key_pool.account_name("kimi", "sk-kimi-abcdef1234")
        self.assertNotIn("abcdef1234", name)
        self.assertTrue(name.endswith("1234"))
        self.assertEqual(plan_key_pool.account_ref("sk-a"), plan_key_pool.account_ref("sk-a"))
        self.assertNotEqual(plan_key_pool.account_ref("sk-a"), plan_key_pool.account_ref("sk-b"))


if __name__ == "__main__":
    unittest.main()

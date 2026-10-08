"""The shared pool skeleton must behave like the four pools that were copied from.

plan_key_pool, workbuddy, xhx and gemini each hand-rolled the same persisted
pool, and the copies had drifted (a reason masked at 160 characters in one and
300 in another, a UTC ISO stamp in one and localtime in another). This pins
the single implementation in bridges/_account_pool.py so the next pool cannot
drift again: the state file, the cooldown bookkeeping, the
primary/active/LRU order, and the summary rows.
"""

import asyncio
import json
import os
import sys
import time
import unittest
from pathlib import Path

BRIDGES = Path(__file__).resolve().parent.parent / "bridges"
sys.path.insert(0, str(BRIDGES))

import _account_pool as poolmod  # noqa: E402


class FakeCandidate(poolmod.AccountCandidate):
    """The per-call object a bridge hands its request path."""

    def __init__(self, ref, key, label):
        self.ref = ref
        self._key = key
        self.label = label

    @property
    def key(self):
        return self._key


class FakePool(poolmod.AccountPool):
    """The minimum a bridge subclass must supply.

    A bridge scans a real directory, so this one does too: <ref>.key holds
    the secret, and scan() reads the directory back. That way a test can
    remove a file and watch the pool drop the account, the way
    removing a key file drops a kimi-code account.
    """

    def __init__(self, auth_dir, refs, **kw):
        self._refs = list(refs)
        super().__init__(auth_dir, **kw)

    def scan(self):
        out = []
        try:
            names = sorted(self.auth_dir.glob("*.key"))
        except OSError:
            return out
        for path in names:
            try:
                out.append({"ref": path.stem, "key": path.read_text(encoding="utf-8").strip(),
                            "name": path.stem})
            except OSError:
                continue
        return out

    def candidate_factory(self, row):
        return FakeCandidate(row["ref"], row["key"], row.get("name") or row["ref"])


class BasePoolTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.auth_dir = Path(self.tmp.name) / "auths"
        self.refs = ["aaa", "bbb", "ccc"]
        for ref in self.refs:
            self.auth_dir.mkdir(parents=True, exist_ok=True)
            (self.auth_dir / (ref + ".key")).write_text(ref.upper(), encoding="utf-8")
        self.pool = FakePool(self.auth_dir, self.refs)

    def _drop(self, ref):
        (self.auth_dir / (ref + ".key")).unlink()

    def tearDown(self):
        self.tmp.cleanup()

    def test_state_file_lives_beside_the_credentials(self):
        self.assertTrue((self.auth_dir / "pool-state.json").is_file())
        stored = json.loads((self.auth_dir / "pool-state.json").read_text())
        self.assertEqual(set(stored["accounts"]), set(self.refs))
        self.assertEqual(stored["primary_ref"], self.refs[0])

    def test_points_reader_is_called_once_per_account(self):
        seen = []

        def read_points(row):
            seen.append(row["key"])
            return {"points": 7, "unit": "credits"}

        self.pool.set_points_reader(read_points)
        rows = asyncio.run(self.pool.refresh_points(force=True))["accounts"]
        self.assertEqual(sorted(seen), ["AAA", "BBB", "CCC"])
        self.assertEqual([r["points"] for r in rows], [7, 7, 7])

    def test_a_failed_points_read_does_not_take_the_pool_down(self):
        def read_points(key):
            raise RuntimeError("balance endpoint 503")

        self.pool.set_points_reader(read_points)
        rows = asyncio.run(self.pool.refresh_points(force=True))["accounts"]
        for row in rows:
            self.assertIsNone(row["points"])
            self.assertIn("balance endpoint 503", row["points_error"])

    def test_primary_then_active_then_least_recently_used(self):
        self.pool.set_primary("ccc")
        self.pool.mark_success("bbb")
        self.pool.mark_success("aaa")
        order = [c.ref for c in self.pool.candidates()]
        self.assertEqual(order, ["ccc", "aaa", "bbb"])

    def test_a_cooled_account_is_skipped_and_returns_after_cooldown(self):
        self.pool.mark_failure("aaa", "insufficient_points", 60)
        self.assertNotIn("aaa", [c.ref for c in self.pool.candidates()])
        self.assertIn("aaa", [c.ref for c in self.pool.candidates(ignore_cooldown=True)])
        self.pool._state["accounts"]["aaa"]["cooldown_until"] = 0
        self.assertIn("aaa", [c.ref for c in self.pool.candidates()])

    def test_mark_success_clears_the_cooldown_and_the_reason(self):
        self.pool.mark_failure("bbb", "401 unauthorized", 60)
        self.pool.mark_success("bbb")
        state = self.pool._state["accounts"]["bbb"]
        self.assertEqual(state["cooldown_until"], 0)
        self.assertEqual(state["reason"], "")
        self.assertEqual(state["failures"], 0)
        self.assertEqual(self.pool._state["active_ref"], "bbb")

    def test_mark_failure_bounds_the_cooldown_at_one_second(self):
        self.pool.mark_failure("aaa", "429 rate limited", 0)
        state = self.pool._state["accounts"]["aaa"]
        self.assertGreater(state["cooldown_until"], 0)
        self.assertEqual(state["failures"], 1)

    def test_status_rows_carry_the_panel_fields(self):
        self.pool.mark_failure("ccc", "402 payment required", 60)
        rows = {r["ref"]: r for r in self.pool.status()}
        self.assertEqual(rows["ccc"]["state"], "cooling")
        self.assertEqual(rows["aaa"]["state"], "ready")
        self.assertEqual(rows["ccc"]["failures"], 1)
        self.assertEqual(rows["ccc"]["reason"], "402 payment required")
        self.assertTrue(rows["aaa"]["primary"])
        self.assertFalse(rows["aaa"]["active"])

    def test_removing_the_file_removes_the_account(self):
        self._drop("ccc")
        self.pool.reload()
        rows = [r["ref"] for r in self.pool.status()]
        self.assertEqual(rows, ["aaa", "bbb"])

    def test_a_corrupt_state_file_leaves_a_usable_pool(self):
        (self.auth_dir / "pool-state.json").write_text("{not json", encoding="utf-8")
        reloaded = FakePool(self.auth_dir, self.refs)
        self.assertEqual([c.ref for c in reloaded.candidates()], ["aaa", "bbb", "ccc"])

    def test_a_primary_that_disappeared_repoints_to_the_first_account(self):
        self.pool.set_primary("ccc")
        self._drop("bbb")
        self._drop("ccc")
        self.pool.reload()
        self.assertEqual(self.pool._state["primary_ref"], "aaa")

    def test_points_stale_follows_the_ttl(self):
        # an account whose balance was never read is stale by definition:
        # points_ts stays 0, so "now - 0" is already past the TTL
        self.assertTrue(self.pool.points_stale())
        now = time.time()
        for ref in self.refs:
            self.pool._state_of(ref)["points_ts"] = now
        self.assertFalse(self.pool.points_stale())
        # a single account older than the TTL makes the whole pool stale, so
        # one refresh() covers the newest reader's window
        self.pool._state_of("aaa")["points_ts"] = now - self.pool.points_ttl - 1
        self.assertTrue(self.pool.points_stale())
        # an empty pool is never stale: there is nothing to re-read
        for ref in self.refs:
            self._drop(ref)
        self.pool.reload()
        self.assertFalse(self.pool.points_stale())

    def test_summary_counts_are_ready_and_cooling(self):
        self.pool.mark_failure("bbb", "500 upstream", 60)
        summary = self.pool.summary()
        self.assertEqual(summary["count"], 3)
        self.assertEqual(summary["ready"], 2)
        self.assertEqual(summary["cooling"], 1)
        self.assertEqual(summary["auth_dir"], str(self.pool.auth_dir))

    def test_the_credential_dir_is_not_world_readable(self):
        if os.name == "nt":
            self.skipTest("chmod is a no-op on Windows")
        mode = self.auth_dir.stat().st_mode & 0o777
        self.assertEqual(mode, 0o700)
        mode = (self.auth_dir / "pool-state.json").stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

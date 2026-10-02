# The gemini bridge used to have exactly one identity: whatever Google account
# happened to be logged in. Rotating between accounts meant copying token files
# by hand, and a single burned/expired identity took the whole bridge down with
# a 502 that never named the credential.
#
# These tests pin the pool that replaced it. They are offline: no OAuth, no
# network, just a tmpdir shaped like ~/.gemini2codex/auths plus the official
# token file. What matters and is easy to regress:
#
#   * with no imported account, the pool is a single "legacy" account that
#     still points at the real login files, so single-account users see zero
#     behaviour change;
#   * the first imported account becomes primary and sorts first;
#   * a failing account goes on cooldown and is skipped, while
#     candidates(ignore_cooldown=True) still offers it (last resort);
#   * the legacy account cannot be removed -- it is not a directory to delete.
import importlib.util
import json
import os
import sys
import tempfile

import pytest

BRIDGES = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, "bridges"))
sys.path.insert(0, BRIDGES)


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(BRIDGES, rel))
    mod = importlib.util.module_from_spec(spec)
    # Register before exec: dataclasses on 3.14 resolves cls.__module__ through
    # sys.modules, so a module loaded by spec alone is not enough.
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


ga = _load("gemini_accounts", os.path.join("gemini", "gemini_accounts.py"))


def _write_token(path, email, expiry="2030-01-01T00:00:00"):
    payload = {"token": {"access_token": "at-" + email, "refresh_token": "rt-" + email,
                         "expiry": expiry}, "auth_method": "oauth"}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    return path


class _Env:
    # A tmpdir holding the official login files plus a fresh auths/ dir.
    def __init__(self):
        self.tmp = tempfile.mkdtemp()
        self.token = _write_token(os.path.join(self.tmp, "jetski-standalone-oauth-token"), "main")
        self.cookie = os.path.join(self.tmp, "cookies.txt")
        with open(self.cookie, "w", encoding="utf-8") as f:
            f.write("placeholder")
        self.auths = os.path.join(self.tmp, "auths")

    def pool(self):
        return ga.GeminiAccountPool(self.auths, legacy_token=self.token, legacy_cookie=self.cookie)

    def add(self, label, expiry="2030-01-01T00:00:00"):
        src = _write_token(os.path.join(self.tmp, label + ".json"), label, expiry)
        return self.pool().import_current(src, None, label)


def test_legacy_account_is_the_only_default_and_points_at_the_real_files():
    env = _Env()
    pool = env.pool()
    rows = pool.status()
    assert [r["label"] for r in rows] == ["legacy"]
    assert rows[0]["legacy"] is True
    assert rows[0]["state"] == "ready"
    cand = pool.candidates()[0]
    assert cand.token_file_for_bridge() == env.token
    assert cand.cookie_file_for_bridge() == env.cookie


def test_first_imported_account_becomes_primary_and_sorts_first():
    env = _Env()
    env.add("gmail-b")
    env.add("gmail-a")
    pool = env.pool()
    assert [c.label for c in pool.candidates()] == ["gmail-b", "gmail-a", "legacy"]
    assert pool.status()[0]["primary"] is True
    assert pool.status()[0]["label"] == "gmail-b"


def test_set_primary_reorders_without_touching_the_others():
    env = _Env()
    ref_a = env.add("gmail-a")["ref"]
    env.add("gmail-b")
    pool = env.pool()
    pool.set_primary(ref_a)
    assert [c.label for c in pool.candidates()] == ["gmail-a", "gmail-b", "legacy"]
    assert pool.status()[0]["label"] == "gmail-a"
    with pytest.raises(KeyError):
        pool.set_primary("0" * 16)


def test_cooldown_skips_the_account_but_ignore_cooldown_still_offers_it():
    env = _Env()
    ref = env.add("gmail-a")["ref"]
    env.add("gmail-b")
    pool = env.pool()
    pool.mark_failure(ref, "code_assist: HTTP 403 PERMISSION_DENIED", 300)
    assert [c.label for c in pool.candidates()] == ["gmail-b", "legacy"]
    # Last-resort mode still leads with the primary; a cooling account is a
    # worse option than a healthy one, not a reason to reorder priorities.
    assert [c.label for c in pool.candidates(ignore_cooldown=True)] == ["gmail-a", "gmail-b", "legacy"]
    row = [r for r in pool.status() if r["ref"] == ref][0]
    assert row["state"] == "cooling"
    assert row["failures"] == 1
    assert "403" in (row["reason"] or "")


def test_mark_success_clears_cooldown_and_becomes_active():
    env = _Env()
    ref = env.add("gmail-a")["ref"]
    pool = env.pool()
    pool.mark_failure(ref, "boom", 300)
    assert pool.status()[0]["state"] == "cooling"
    pool.mark_success(ref)
    row = pool.status()[0]
    assert row["state"] == "ready"
    assert row["active"] is True
    assert row["failures"] == 0


def test_primary_always_wins_and_lru_orders_the_rest():
    env = _Env()
    ref_a = env.add("gmail-a")["ref"]
    ref_b = env.add("gmail-b")["ref"]
    ref_legacy = ga._ref("legacy")
    pool = env.pool()
    pool.set_primary(ref_a)
    pool.mark_success(ref_b)
    # gmail-a is the chosen primary; b just worked, legacy never has.
    assert [c.label for c in pool.candidates()] == ["gmail-a", "gmail-b", "legacy"]
    pool.mark_success(ref_legacy)
    # Same primary, but the leftover accounts swapped by recency of use.
    assert [c.label for c in pool.candidates()] == ["gmail-a", "legacy", "gmail-b"]


def test_remove_drops_the_account_but_never_the_legacy_one():
    env = _Env()
    ref = env.add("gmail-a")["ref"]
    pool = env.pool()
    pool.remove(ref)
    assert [r["label"] for r in pool.status()] == ["legacy"]
    assert not os.path.isdir(os.path.join(env.auths, "gmail-a"))
    with pytest.raises(RuntimeError):
        pool.remove(ga._ref("legacy"))
    assert [r["label"] for r in pool.status()] == ["legacy"]


def test_remove_an_unknown_account_is_a_keyerror():
    env = _Env()
    with pytest.raises(KeyError):
        env.pool().remove("f" * 16)


@pytest.mark.parametrize("label", ["", "   ", "a/b", "..", ".", ".hidden", "legacy"])
def test_import_rejects_unusable_labels(label):
    env = _Env()
    with pytest.raises(ValueError):
        env.pool().import_current(env.token, None, label)
    # No phantom credential may appear in the pool from a rejected label.
    assert [r["label"] for r in env.pool().status()] == ["legacy"]


def test_import_without_any_token_available_is_an_error_not_a_silent_account():
    env = _Env()
    missing = os.path.join(env.tmp, "does-not-exist.json")
    with pytest.raises(RuntimeError):
        env.pool().import_current(missing, None, "gmail-a")
    assert [r["label"] for r in env.pool().status()] == ["legacy"]


def test_token_only_account_has_no_web_cookie_so_channel_b_is_not_guessed():
    env = _Env()
    env.add("gmail-a")
    cand = env.pool().candidates()[0]
    assert cand.cookie_path is None
    fallback = cand.cookie_file_for_bridge()
    assert fallback.endswith("cookies.txt")
    assert not os.path.exists(fallback)


def test_primary_and_active_survive_a_restart():
    env = _Env()
    ref_b = env.add("gmail-b")["ref"]
    pool = env.pool()
    pool.set_primary(ref_b)
    pool.mark_failure(ref_b, "boom", 300)
    reopened = ga.GeminiAccountPool(env.auths, legacy_token=env.token, legacy_cookie=env.cookie)
    assert reopened.status()[0]["label"] == "gmail-b"
    assert reopened.status()[0]["primary"] is True
    assert reopened.status()[0]["state"] == "cooling"


def test_expired_token_is_reported_as_expired_not_ready():
    env = _Env()
    ref = env.add("gmail-a", expiry="2020-01-01T00:00:00")["ref"]
    row = env.pool().get_account(ref)
    assert row["state"] == "expired"
    assert row["token_expired"] is True


def test_summary_counts_the_pool_for_the_status_panel():
    env = _Env()
    ref = env.add("gmail-a")["ref"]
    pool = env.pool()
    pool.mark_failure(ref, "boom", 300)
    s = pool.summary()
    assert s["ok"] is True
    assert s["count"] == 2
    assert s["ready"] == 1
    assert s["cooling"] == 1
    assert s["auth_dir"] == os.path.abspath(env.auths)
    assert [a["label"] for a in s["accounts"]] == ["gmail-a", "legacy"]

"""The real-call prover must resolve bridge keys by the labels its table uses.

Measured 2026-09-29: verify_one looked the BRIDGES bare suffix ("qwen2codex")
up in the dict plist_keys() returns, which service_keys() keys by full labels
("com.local.qwen2codex"). The lookup never matched, so the prober called six
bridges with no key at all and reported the key-enforcing ones AUTH_EXPIRED
("session/key 失效，需重新登录") -- while qoder answered a plain chat 200 with
its real key minutes later. index_keys pins both spellings without letting a
bare entry shadow a full label.
"""
import importlib.util
import os

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "verify_real_calls", os.path.join(HERE, "verify_real_calls.py"))
vrc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vrc)


def test_a_bare_suffix_resolves_to_the_full_label_key():
    full = {"com.local.qwen2codex": "sk-qwen", "com.local.trae2codex": "sk-trae"}
    idx = vrc.index_keys(full)
    assert idx["com.local.qwen2codex"] == "sk-qwen"
    assert idx["qwen2codex"] == "sk-qwen"
    assert idx["trae2codex"] == "sk-trae"


def test_full_labels_stay_authoritative():
    """A bare entry already in the dict must not shadow the real label."""
    full = {"com.local.qwen2codex": "sk-real", "qwen2codex": "sk-decoy"}
    idx = vrc.index_keys(full)
    assert idx["qwen2codex"] == "sk-real"
    assert idx["com.local.qwen2codex"] == "sk-real"


def test_every_bridges_row_can_resolve_a_key_from_the_machine():
    """Where plists exist, each table row finds its key (vacuously true in CI)."""
    keys = vrc.plist_keys("com.local")
    if not keys:
        pytest.skip("no installed *2codex services on this machine")
    for name, label, _offset, _pref, _keyenv in vrc.BRIDGES:
        assert label in keys, "%s (%s) has no key in the indexed dict" % (name, label)

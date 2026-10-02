"""The prover must outlive a bridge whose own budget is larger than the probe.

verify_real_calls.py probes every chat call with PROBE_CHAT_TIMEOUT (70s).
zcode drives the official CLI (ZCODE_CLI_TIMEOUT, default 180s) and mints an
Aliyun captcha first (up to 75s), so a 70s probe reports BRIDGE_DOWN -- a
timeout -- for a bridge that is merely slow. That is a different verdict from
the truth, and it hides the real upstream reason.

A per-bridge override fixes that, but passing the kwarg on every call broke ten
tests whose fakes patch chat() with a narrower lambda. The override is therefore
only passed when one exists, so every other bridge keeps the historical
signature; the second test pins exactly that.
"""
import importlib.util
import os
import sys
import unittest

import pytest

KIT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
spec = importlib.util.spec_from_file_location(
    "vrc_timeout_override", os.path.join(KIT, "tools", "verify_real_calls.py"))
vrc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vrc)

BRIDGE_NAMES = {row[0] for row in vrc.BRIDGES}


def test_only_bridges_with_a_larger_own_budget_are_overridden():
    override = vrc.PROBE_CHAT_TIMEOUT_OVERRIDE
    assert override, "the override table is empty"
    for name, timeout in override.items():
        assert timeout > vrc.PROBE_CHAT_TIMEOUT, name
        assert name in BRIDGE_NAMES, name


def test_a_bridge_without_an_override_keeps_the_plain_call_signature():
    """No kwarg is sent unless a bridge overrides one.

    A fake chat() narrower than the real signature is what several existing
    tests install, and passing timeout= unconditionally broke all of them.
    """
    names = sorted(BRIDGE_NAMES - set(vrc.PROBE_CHAT_TIMEOUT_OVERRIDE))
    assert names, "every bridge overrides; nothing pins the plain signature"
    probe = names[0]
    calls = []

    def fake_chat(port, model, key, content, max_tokens=2048):
        calls.append(model)
        return dict(code=200, secs=0.1, text="AB12 123", rmodel=model,
                    err="", usage={})

    row = next(r for r in vrc.BRIDGES if r[0] == probe)
    with pytest.MonkeyPatch.context() as mp:
        # get_models has to answer before any chat call is reached.
        mp.setattr(vrc, "get_models", lambda port, key: [row[3]])
        mp.setattr(vrc, "chat", fake_chat)
        vrc.verify_one(row[0], row[1], row[2], row[3], row[4], 8787,
                       {row[1]: "sk-test"})
    assert calls, "verify_one never called chat()"


if __name__ == "__main__":
    unittest.main()

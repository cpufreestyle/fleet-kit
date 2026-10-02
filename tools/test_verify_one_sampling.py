"""A bridge must be graded on more than the first four models it lists.

Measured 2026-09-29, 20:26 run: verify_one() built its candidate list with
cands[:4] and reported the LAST failure as the whole bridge verdict. cline2codex
serves the same 14 models on every call; that run happened to sample four of
them through the macOS system proxy, got InvalidProxyMessage in 0.01s on each,
and filed the bridge UPSTREAM_DOWN -- while --only cline on the same port
answered REAL, HTTP 200, 8.14s, 35 minutes later. A sample of 1-4 models cannot
represent a 14-model catalogue, and the model the BRIDGES table names was not
even guaranteed a slot when /v1/models returned a stale list.

These tests pin the three guarantees that replace it: the declared model is
always attempted first, the sample is wide enough to reach a working model in a
mostly-broken catalogue, and every attempt is recorded so a thin sample shows
itself.
"""
import importlib.util
import os
import re

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "verify_real_calls", os.path.join(HERE, "verify_real_calls.py"))
vrc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vrc)


def _answer(content):
    """Build the reply a real model gives to verify_real_calls.make_probe."""
    m = re.search(r"\((\d+)\+(\d+)", content)
    n, add = int(m.group(1)), int(m.group(2))
    nonce = re.search(r"暗号：(\S+)", content).group(1)
    return "%s %d" % (nonce[:4], n + add)


def _chat_for(broken, missing):
    def chat(port, model, key, content, max_tokens=2048):
        if model in broken:
            return dict(code=502, secs=0.01, text="", rmodel="", usage={},
                        err="InvalidProxyMessage: did not receive")
        if model in missing:
            return dict(code=400, secs=0.2, text="", rmodel="", usage={},
                        err="model not found")
        return dict(code=200, secs=3.0, text=_answer(content), rmodel=model,
                    err="", usage={})
    return chat


def _stream_ok(port, model, key):
    """A bridge whose SSE works; these tests are about the sample, not SSE."""
    return dict(code=200, secs=0.3, ctype="text/event-stream",
                data="data: {}", err="")


def _probe(ids, broken=(), missing=(), preferred="bridge/declared-model"):
    """Run verify_one against a fake bridge exposing exactly ids."""
    vrc.get_models = lambda port, key: list(ids)
    vrc.chat = _chat_for(broken, missing)
    vrc.stream_probe = _stream_ok
    return vrc.verify_one("fake", "com.local.fake", 3, preferred, "FAKE_KEY",
                          8787, {})


_PATCHED = ("get_models", "chat", "stream_probe")
_ORIGINALS = dict((n, getattr(vrc, n)) for n in _PATCHED)


@pytest.fixture(autouse=True)
def _restore_module_attrs():
    """These tests rebind module globals; do not leak them into other modules."""
    yield
    for name, fn in _ORIGINALS.items():
        setattr(vrc, name, fn)


def test_the_declared_model_is_always_attempted_first():
    row = _probe(["good/a", "good/b"], missing=("bridge/declared-model",))
    assert row["attempted"][0]["model"] == "bridge/declared-model"
    assert row["verdict"] == "REAL"


def test_a_broken_sample_cannot_condemn_a_working_bridge():
    broken = ["broken/%d" % i for i in range(4)]
    row = _probe(broken + ["good/only"], broken=broken,
                 missing=("bridge/declared-model",))
    assert row["verdict"] == "REAL"
    assert row["model"] == "good/only"


def test_every_attempt_is_recorded_with_its_code():
    broken = ["broken/%d" % i for i in range(3)]
    row = _probe(broken + ["good/only"], broken=broken,
                 missing=("bridge/declared-model",))
    tried = [(a["model"], a["code"]) for a in row["attempted"]]
    assert ("bridge/declared-model", 400) in tried
    assert ("broken/0", 502) in tried
    assert ("good/only", 200) in tried


def test_a_bridge_that_really_is_down_says_so():
    broken = ["broken/%d" % i for i in range(6)]
    row = _probe(broken, broken=broken, missing=("bridge/declared-model",))
    assert row["verdict"] == "UPSTREAM_DOWN"
    assert len(row["attempted"]) == vrc.MAX_CANDIDATES
    assert "共试%d个模型" % vrc.MAX_CANDIDATES in row["note"]

def test_the_sample_is_capped_so_a_broken_bridge_costs_bounded_calls():
    broken = ["broken/%d" % i for i in range(30)]
    row = _probe(broken, broken=broken, missing=("bridge/declared-model",))
    assert len(row["attempted"]) == vrc.MAX_CANDIDATES

def test_no_candidate_at_all_is_bridge_down_not_a_sample():
    row = _probe([], preferred="")
    assert row["verdict"] == "BRIDGE_DOWN"
    assert row["attempted"] == []


def test_a_proxy_hop_failure_is_not_reported_as_an_upstream_outage():
    v, note = vrc.classify(502, 0.01, "", "", 0, 0,
                          "InvalidProxyMessage: did not receive a response")
    assert v == "UPSTREAM_DOWN"
    assert "本地代理隧道异常" in note


def test_a_plain_502_keeps_the_upstream_wording():
    v, note = vrc.classify(502, 5.0, "", "", 0, 0, "bad gateway from upstream")
    assert v == "UPSTREAM_DOWN"
    assert "本地代理" not in note

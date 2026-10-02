"""A 403 that names the plan is not an expired session, and it must not end the walk.

Measured 2026-09-29 21:30 with --only lingxi on port 8792. The bridge answered
403 in 0.09s with body {"error":"model not allowed on your current plan:
deepseek-v4-flash"}, /health still reported session_alive=true, and
lingxi/deepseek-flash answered 200 in 0.97s with the probe arithmetic right. So
the login was fine and exactly one catalogued model was off-plan -- yet
verify_one graded the whole bridge AUTH_EXPIRED on that single first call,
because classify() mapped 401 and 403 to the same verdict and returned
immediately instead of trying the next candidate.

These tests pin the split: a plan/quota verb in the body is reported as
PLAN_BLOCKED, a silent 403 stays AUTH_EXPIRED, and the walk only stops early on
a verdict that really is account-wide (401, or a 403 with no plan verb).
"""
import importlib.util
import json
import os
import re

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "verify_real_calls", os.path.join(HERE, "verify_real_calls.py"))
vrc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vrc)

PLAN_403 = ('{"error":"model not allowed on your current plan: '
            'lingxi/deepseek-v4-flash"}')

_PATCHED = ("get_models", "chat", "stream_probe")
_ORIGINALS = dict((n, getattr(vrc, n)) for n in _PATCHED)


@pytest.fixture(autouse=True)
def _restore_module_attrs():
    """These tests rebind module globals; do not leak them into other modules."""
    yield
    for name, fn in _ORIGINALS.items():
        setattr(vrc, name, fn)


def _stream_ok(port, model, key):
    """A bridge whose SSE works; these tests are about the 403, not SSE.

    Without this stub the real stream_probe dials the port passed in -- 8787,
    a live bridge -- so the test would talk to production and could grade a REAL
    row down to STREAM_BROKEN on a failure that has nothing to do with the 403.
    """
    return dict(code=200, secs=0.3, ctype="text/event-stream",
                data="data: {}", err="")


def _answer(content):
    m = re.search(r"\((\d+)\+(\d+)", content)
    n, add = int(m.group(1)), int(m.group(2))
    nonce = re.search(r"暗号：(\S+)", content).group(1)
    return "%s %d" % (nonce[:4], n + add)


def test_plan_403_is_not_an_expired_session():
    v, note = vrc.classify(403, 0.09, "", "ABCD", 1, 2, PLAN_403)
    assert v == "PLAN_BLOCKED"
    assert "model not allowed" in note


def test_silent_403_is_still_an_expired_session():
    v, note = vrc.classify(403, 0.2, "", "ABCD", 1, 2, "forbidden")
    assert v == "AUTH_EXPIRED"
    assert note.startswith("403")


def test_401_stays_auth_expired_even_with_plan_wording():
    v, _ = vrc.classify(401, 0.2, "", "ABCD", 1, 2, PLAN_403)
    assert v == "AUTH_EXPIRED"


def test_quota_403_is_plan_blocked_too():
    for body in ("monthly quota exceeded", "insufficient credits",
                 "permission denied for this model", "当前套餐不含该模型",
                 "配额已用尽"):
        v, _ = vrc.classify(403, 0.1, "", "ABCD", 1, 2, body)
        assert v == "PLAN_BLOCKED", body


KIMI_403 = ("""{"error":{"message":"Your current subscription does not have "
            "access to Kimi Code right now. Upgrade your plan to keep coding "
            "with Kimi Code: https://www.kimi.com/code/#pricing","type":"
            "access_terminated_error"}}""")


def test_a_lapsed_kimi_plan_is_not_an_expired_session():
    """The kimi bridge answers this exact body, measured 2026-10-03.

    Its key is accepted -- /v1/models still answers 200 with it -- and only
    the coding plan has lapsed. Reporting AUTH_EXPIRED sends the operator to
    regenerate a credential that is already fine; the renewal page is the
    answer they need.
    """
    v, note = vrc.classify(403, 0.2, "", "ABCD", 1, 2, KIMI_403)
    assert v == "PLAN_BLOCKED"
    # the note is the plan wording plus the first 44 chars of the body,
    # so what is pinned is that it took the plan path at all
    assert "套餐受限" in note
    assert "Your current subscri" in note


def test_the_gateway_wording_for_the_same_case_is_plan_blocked_too():
    """The bridge restates it in its own words; that must classify the same."""
    body = ("""{"error":{"message":"kimi code has no active plan for this "
             "key (the key itself is accepted): ... -- renew at "
             "https://www.kimi.com/code/#pricing","type":"
             "kimi_plan_inactive"}}""")
    v, _note = vrc.classify(403, 0.2, "", "ABCD", 1, 2, body)
    assert v == "PLAN_BLOCKED"


def test_a_missing_upstream_key_is_not_an_upstream_outage():
    """The minimax bridge answers 503 key_missing before dialing the vendor.

    Measured 2026-10-03: minimax has no MINIMAX_API_KEY on this machine, and
    verify_real_calls graded it UPSTREAM_DOWN, which sends the operator to the
    vendor status page for a key that was never set. The fix is a fleet.env
    line, not a vendor incident.
    """
    body = json.dumps({"error": {
        "message": "minimax bridge has no MINIMAX_API_KEY; set a MiniMax "
                   "pay-as-you-go key in fleet.env and re-run bash "
                   "bridges/finish.sh minimax",
        "type": "minimax_key_missing"}})
    v, note = vrc.classify(503, 0.0, "", "ABCD", 1, 2, body)
    assert v == "NO_KEY"
    assert "finish.sh" in note


def test_off_plan_model_does_not_sink_the_bridge():
    """The lingxi shape: declared model off-plan, second candidate answers."""
    def chat(port, model, key, content, max_tokens=2048):
        if "deepseek-v4-flash" in model:
            return dict(code=403, secs=0.09, text="", rmodel="", usage={},
                        err=PLAN_403)
        return dict(code=200, secs=1.1, text=_answer(content), rmodel=model,
                    err="", usage={})

    vrc.get_models = lambda port, key: ["lingxi/deepseek-v4-flash",
                                        "lingxi/deepseek-flash"]
    vrc.chat = chat
    vrc.stream_probe = _stream_ok
    row = vrc.verify_one("lingxi", "com.local.lingxi2codex", 5,
                         "lingxi/deepseek-v4-flash", "LINGXI2CODEX_KEY", 8787, {})
    assert row["verdict"] == "REAL"
    assert row["model"] == "lingxi/deepseek-flash"
    assert [a["code"] for a in row["attempted"]] == [403, 200]


def test_every_model_off_plan_reports_plan_blocked_with_the_sample_size():
    def chat(port, model, key, content, max_tokens=2048):
        return dict(code=403, secs=0.09, text="", rmodel="", usage={},
                    err=PLAN_403)

    vrc.get_models = lambda port, key: ["a/one", "a/two", "a/three"]
    vrc.chat = chat
    row = vrc.verify_one("a", "com.local.a", 3, "a/preferred", "A_KEY", 8787, {})
    assert row["verdict"] == "PLAN_BLOCKED"
    assert len(row["attempted"]) == 4          # preferred + the three ids
    assert "共试4个模型" in row["note"]


def test_off_plan_does_not_burn_the_budget_ladder():
    """Escalating max_tokens cannot fix a plan rejection -- one call per model."""
    calls = []

    def chat(port, model, key, content, max_tokens=2048):
        calls.append((model, max_tokens))
        return dict(code=403, secs=0.09, text="", rmodel="", usage={},
                    err=PLAN_403)

    vrc.get_models = lambda port, key: ["a/one", "a/two"]
    vrc.chat = chat
    row = vrc.verify_one("a", "com.local.a", 3, "a/preferred", "A_KEY", 8787, {})
    assert len(calls) == 3
    assert sorted(set(b for _, b in calls)) == [256]

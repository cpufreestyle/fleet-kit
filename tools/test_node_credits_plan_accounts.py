"""Kimi Code and MiniMax owe the same three answers as any bridge node.

The fleet runs no bridge for either plan, so the credits view has to ask
them directly: is the account alive, which key is behind it, and what does
the platform say is left. A refused key must read as refused -- a dead key
rendered as zero credits is how a paid plan gets cancelled unnoticed.
"""
import json

import node_credits as nc
import plan_credits

USAGES = {"usages": [{"window": "5h", "remaining": 128, "limit": 300}]}
KIMI_OK = (200, {"endpoint": "https://api.kimi.com/coding/v1/usages",
                "body": USAGES})
KIMI_DEAD = (401, {"endpoint": "https://api.kimi.com/coding/v1/usages", "body": {"error": {"message": "invalid or expired"}}})
MINIMAX_OK = (200, {"endpoint": "https://api.minimaxi.com/v1/chat/completions",
                     "model": "MiniMax-M2"})


def _isolate(monkeypatch, key="sk-live-1234567890"):
    monkeypatch.delenv("KIMI_CODING_API_KEY", raising=False)
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    monkeypatch.setattr(nc, "cc_switch_kimi_key", lambda: key)


def test_kimi_row_reads_the_usage_windows(monkeypatch):
    _isolate(monkeypatch)
    monkeypatch.setattr(plan_credits, "kimi", lambda k: KIMI_OK)
    row = nc.read_node("kimi-code")
    assert row["up"] is True
    assert row["logged_in"] is True
    assert "remaining=128" in row["credits_note"]
    assert row["credits_value"] == 128
    assert row["credits_unit"] == "remaining"
    assert "v1/usages" in row["detail"]
    assert "cc-switch default provider" in row["credits_source"]


def test_kimi_row_reports_a_refused_key_as_refused(monkeypatch):
    _isolate(monkeypatch)
    monkeypatch.setattr(plan_credits, "kimi", lambda k: KIMI_DEAD)
    row = nc.read_node("kimi-code")
    assert row["up"] is False
    assert row["logged_in"] is False
    assert "key 被拒" in row["credits_note"]
    assert "invalid or expired" in row["credits_note"]
    assert row["credits_value"] is None


def test_minimax_row_reports_the_call_it_made(monkeypatch):
    _isolate(monkeypatch)
    monkeypatch.setenv("MINIMAX_API_KEY", "mm-key-1234")
    monkeypatch.setattr(plan_credits, "minimax", lambda k: MINIMAX_OK)
    row = nc.read_node("minimax-code")
    assert row["up"] is True
    assert row["credits_kind"] == "subscription"
    assert "控制台" in row["credits_note"]
    assert "chat/completions" in row["detail"]
    assert row["credits_value"] is None


def test_no_credential_makes_no_call(monkeypatch):
    _isolate(monkeypatch, key="")
    monkeypatch.setattr(plan_credits, "kimi",
                        lambda k: (_ for _ in ()).throw(
                            AssertionError("no key means no call")))
    row = nc.read_node("kimi-code")
    assert row["up"] is False
    assert "未配置 key" in row["credits_note"]
    assert "未发起调用" in row["detail"]


def test_plan_nodes_ride_the_same_order_as_the_bridges():
    assert "kimi-code" in nc.NODE_ORDER
    assert "minimax" in nc.NODE_ORDER
    assert nc.VENDORS["minimax"]

def test_a_key_shared_with_another_vendor_is_named_as_the_reason(monkeypatch):
    _isolate(monkeypatch, key="shared-placeholder-token")
    monkeypatch.setattr(nc, "cc_switch_key_reuse", lambda: {
        "shared-placeholder-token": [("claude/default", "https://api.kimi.com/coding/"),
                                    ("codex/OpenAI Official", "api.openai.com 官方")]})
    monkeypatch.setattr(plan_credits, "kimi", lambda k: KIMI_DEAD)
    row = nc.read_node("kimi-code")
    assert row["credits_value"] is None
    assert "key 被拒" in row["credits_note"]
    assert "codex/OpenAI Official" in row["credits_note"]
    assert "不是 Kimi 的 key" in row["credits_note"]
    assert "codex/OpenAI Official" in row["detail"]


def test_a_kimi_endpoint_token_is_never_called_a_placeholder(monkeypatch):
    _isolate(monkeypatch)
    monkeypatch.setattr(nc, "cc_switch_key_reuse", lambda: {
        "sk-live-1234567890": [("claude/default", "https://api.kimi.com/coding/")]})
    monkeypatch.setattr(plan_credits, "kimi", lambda k: KIMI_DEAD)
    row = nc.read_node("kimi-code")
    assert row["credits_note"] == "key 被拒：HTTP 401 invalid or expired"
    assert "placeholder" not in row["credits_note"].lower()


def test_a_key_shaped_token_wins_over_the_placeholder_slot(monkeypatch):
    rows = [("claude", "default", "placeholder-token-64-chars",
             "https://api.kimi.com/coding/"),
            ("claude", "real", "sk-kimi-live-key-0001",
             "https://api.kimi.com/coding/")]
    monkeypatch.setattr(nc, "_cc_switch_providers", lambda: rows)
    assert nc.cc_switch_kimi_key() == "sk-kimi-live-key-0001"
    assert nc.cc_switch_kimi_slot("sk-kimi-live-key-0001") == "claude/real"
    assert nc.cc_switch_kimi_slot("placeholder-token-64-chars") == "claude/default"


def test_a_machine_with_no_cc_switch_db_has_no_kimi_key(monkeypatch):
    monkeypatch.setattr(nc, "_cc_switch_providers", lambda: [])
    assert nc.cc_switch_kimi_key() == ""
    assert nc.cc_switch_kimi_slot("anything") == ""
    assert nc.cc_switch_key_reuse() == {}
    assert nc.cc_switch_placeholder_note("anything") == ""

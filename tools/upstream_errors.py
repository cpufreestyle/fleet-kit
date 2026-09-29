"""Signatures of an upstream refusal that still arrives as HTTP 200.

A bridge can be perfectly reachable while its upstream refuses the call.
workbuddy's `11128 Illegal API invocation from an unapproved channel` came
back as a 200 whose message text read like an answer, so a checker that only
asks "did we get text?" called the bridge healthy -- and the picker kept
offering a model that could never respond, with the failure only showing up
inside a live conversation.

Every consumer that judges a reply asks this module before it decides:
fleet_probe.py (reachability -> catalog ordering), verify_real_calls.py
(the REAL verdict -> which rows the catalog filter may hide) and
fleet_chat_test.py. A body that matches any marker below is a refusal, not a
reply, no matter which status code carried it.
"""

# Lowercase substrings. Kept deliberately broad: a false positive costs one
# retried probe, a false negative costs a dead model in the picker.
MARKERS = (
    "unapproved channel",
    "illegal api invocation",
    "channel verification",
    "request blocked",
    "请求被拦截",
    "請求已被攔截",
    "请重新发送",
    "請重新傳送",
    "team not allowed to access model",
    "team_model_access_denied",
    "not allowed for this team key",
    "invalid proxy server token",
    "authentication error",
    "所有供应商已熔断",
    "无可用渠道",
)

VERDICT = "CHANNEL_BLOCKED"
VERDICT_NOTE = "上游渠道校验未通过(非官方客户端/密钥)"

__all__ = ["MARKERS", "VERDICT", "VERDICT_NOTE", "is_error_body", "matched_marker"]


def matched_marker(text):
    """The marker that matched, or None. Case-insensitive, whitespace-tolerant."""
    low = " ".join((text or "").lower().split())
    for marker in MARKERS:
        if marker in low:
            return marker
    return None


def is_error_body(text) -> bool:
    return matched_marker(text) is not None

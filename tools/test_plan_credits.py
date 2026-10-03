"""The credits probe must answer, and must tell a dead key from a dead net.

What is checked here, and why each one earned a test:

* the auth-shape retry, because a key that answers only x-api-key must not
  read as expired;
* the region retry, because a MiniMax key from the international console
  answers 401 against the CN host and that is not a dead key;
* the app-key discovery, because "no arguments needed" is only a feature
  while the file it reads is found rather than assumed;
* the terminated-plan path, because a key that works on a plan that lapsed
  is the one answer the previous code reported as "dead or revoked", which
  sent the user hunting for a key they already had.

No test here touches the network or the real Kimi app storage: the app-key
lookup is redirected to a scratch home and fetch is stubbed, so the suite
cannot report on the developer's own account.
"""
import json
import os
import sys

import pytest

import plan_credits as pc


@pytest.fixture(autouse=True)
def no_real_key_file(monkeypatch, tmp_path):
    """Never read the developer's own Kimi app storage during a test.

    The lookup is pointed at a home that does not exist rather than at a
    function that returns nothing, so a test can still exercise the real
    discovery through app_key_from while main() finds no key.
    """
    empty = os.path.join(str(tmp_path), "no-such-home")
    real = pc.app_key_from
    monkeypatch.setattr(pc, "kimi_app_key",
                        lambda home=None: real(empty))


def _monkey(monkeypatch, status, text):
    calls = []

    def fake(url, headers=None, method="GET", body=None, timeout=25):
        calls.append((url, method, body, headers))
        return status, text

    monkeypatch.setattr(pc, "fetch", fake)
    return calls


def _write_app_key(home, key="sk-kimi-test", user="u1"):
    """The key file where this platform's Kimi desktop app leaves it."""
    rel = next(r for platform, r in pc.KIMI_APP_KEY_FILES
               if sys.platform.startswith(platform))
    path = os.path.join(home, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"v": 2, "keys": [{"userId": user, "apiKey": key,
                                     "keyId": "kid"}]}, fh)
    return path


# ------------------------------------------------------------------- discovery
def test_the_app_key_file_answers_with_no_arguments(tmp_path, monkeypatch, capsys):
    _write_app_key(str(tmp_path))
    monkeypatch.setattr(pc, "kimi_app_key",
                        lambda home=None: pc.app_key_from(str(tmp_path)))
    body = json.dumps({"usages": [{"window": "5h", "remaining": 128}]})
    calls = _monkey(monkeypatch, 200, body)
    assert pc.main(["kimi"]) == 0
    assert calls[0][0] == "https://api.kimi.com/coding/v1/usages"
    assert "128" in capsys.readouterr().out


def _app_key(home):
    return pc.kimi_app_key.__wrapped__(home) if hasattr(pc.kimi_app_key, "__wrapped__") else None


def test_app_key_discovery_reads_the_file_the_app_writes(tmp_path):
    path = _write_app_key(str(tmp_path))
    key, where = pc.app_key_from(str(tmp_path))
    assert key == "sk-kimi-test"
    assert where == path


def test_app_key_discovery_says_why_when_the_file_is_absent(tmp_path):
    key, why = pc.kimi_app_key(str(tmp_path))
    assert key is None
    assert "no kimi-code key" in why


def test_app_key_discovery_survives_a_broken_file(tmp_path):
    path = os.path.join(str(tmp_path), "daimon-share", "daimon")
    os.makedirs(path)
    with open(os.path.join(path, "kimi-code-key.json"), "w",
              encoding="utf-8") as fh:
        fh.write("{not json")
    assert pc.app_key_from(str(tmp_path))[0] is None


def test_no_app_key_flag_stops_the_fallback(tmp_path, monkeypatch, capsys):
    _write_app_key(str(tmp_path))
    monkeypatch.setenv("KIMI_CODING_API_KEY", "")
    assert pc.main(["kimi", "--no-app-key"]) == 3
    assert "no key for kimi" in capsys.readouterr().err


def _no_remains(monkeypatch):
    """Pretend the subscription route has nothing to say.

    main() now tries /v1/token_plan/remains first. These tests are about
    the chat-call fallback, so the route is stubbed out of the way rather
    than the tests being rewritten around a call they never meant to make.
    """
    monkeypatch.setattr(pc, "minimax_remains",
                        lambda key, hosts=None: (401, {"platform": "minimax"}))


# ------------------------------------------------------------------ the answers
def test_kimi_reads_the_usage_windows(monkeypatch, capsys):
    body = json.dumps({"usages": [{"window": "5h", "remaining": 128,
                                  "limit": 300}]})
    calls = _monkey(monkeypatch, 200, body)
    assert pc.main(["kimi", "--key", "sk-test"]) == 0
    url, method, _body, headers = calls[0]
    assert url == "https://api.kimi.com/coding/v1/usages"
    assert method == "GET"
    assert headers["Authorization"] == "Bearer sk-test"
    assert "remaining" in capsys.readouterr().out


def test_kimi_retries_the_other_auth_shape_before_calling_a_key_dead(monkeypatch):
    calls = []

    def fake(url, headers=None, method="GET", body=None, timeout=25):
        calls.append(headers)
        if "Authorization" in headers:
            return 401, json.dumps({"error": {"message": "nope"}})
        return 200, json.dumps({"usages": []})

    monkeypatch.setattr(pc, "fetch", fake)
    assert pc.main(["kimi", "--key", "sk-test"]) == 0
    assert any("x-api-key" in c for c in calls), \
        "the second shape was never tried"


def test_kimi_dead_key_is_exit_2_not_a_crash(monkeypatch, capsys):
    body = json.dumps({"error": {"message": "invalid or expired"}})
    _monkey(monkeypatch, 401, body)
    assert pc.main(["kimi", "--key", "sk-dead"]) == 2
    assert "dead or revoked" in capsys.readouterr().err


def test_a_lapsed_plan_is_exit_4_not_a_dead_key(monkeypatch, capsys):
    """A 403 on the chat call under a working key is a plan problem."""
    seen = []

    def fake(url, headers=None, method="GET", body=None, timeout=25):
        seen.append(url)
        if url.endswith("/v1/usages"):
            return 200, "{}"
        if url.endswith("/v1/me"):
            return 200, json.dumps({"user_level": 10,
                                    "user_level_name": "Free",
                                    "goods_version": 0})
        return 403, json.dumps({"error": {
            "type": "access_terminated_error",
            "message": "Upgrade your plan: https://www.kimi.com/code/#pricing"}})

    monkeypatch.setattr(pc, "fetch", fake)
    assert pc.main(["kimi", "--key", "sk-test"]) == 4
    err = capsys.readouterr().err
    assert "not active" in err
    assert "kimi.com/code/#pricing" in err
    assert any(u.endswith("/v1/chat/completions") for u in seen), \
        "the reason was never asked for"


def test_no_spend_skips_the_chat_call_that_explains_an_empty_plan(monkeypatch):
    seen = []

    def fake(url, headers=None, method="GET", body=None, timeout=25):
        seen.append(url)
        if url.endswith("/v1/usages"):
            return 200, "{}"
        if url.endswith("/v1/me"):
            return 200, json.dumps({"user_level_name": "Free"})
        return 403, "{}"

    monkeypatch.setattr(pc, "fetch", fake)
    assert pc.main(["kimi", "--key", "sk-test", "--no-spend"]) == 0
    assert not any(u.endswith("/v1/chat/completions") for u in seen)


def test_a_windows_body_never_spends_a_token(monkeypatch):
    seen = []

    def fake(url, headers=None, method="GET", body=None, timeout=25):
        seen.append(url)
        if url.endswith("/v1/usages"):
            return 200, json.dumps({"usages": [{"window": "5h"}]})
        return 200, json.dumps({"user_level_name": "Pro"})

    monkeypatch.setattr(pc, "fetch", fake)
    assert pc.main(["kimi", "--key", "sk-test"]) == 0
    assert not any(u.endswith("/v1/chat/completions") for u in seen)


def test_minimax_spends_one_token_with_the_bearer(monkeypatch, capsys):
    _no_remains(monkeypatch)
    body = json.dumps({"choices": [{"message": {"content": "hi"}}]})
    calls = _monkey(monkeypatch, 200, body)
    assert pc.main(["minimax", "--key", "mm-key",
                    "--model", "MiniMax-M2.5"]) == 0
    url, method, req_body, headers = calls[0]
    assert url == "https://api.minimaxi.com/v1/chat/completions"
    assert method == "POST"
    assert req_body["max_tokens"] == 1
    assert req_body["model"] == "MiniMax-M2.5"
    assert headers["Authorization"] == "Bearer mm-key"
    assert capsys.readouterr().out


def test_minimax_retries_the_other_region_before_calling_a_key_dead(monkeypatch, capsys):
    _no_remains(monkeypatch)
    calls = []

    def fake(url, headers=None, method="GET", body=None, timeout=25):
        calls.append(url)
        if "minimaxi.com" in url:
            return 401, json.dumps({"error": {"message": "login fail"}})
        return 200, json.dumps({"choices": [{"message": {"content": "hi"}}]})

    monkeypatch.setattr(pc, "fetch", fake)
    assert pc.main(["minimax", "--key", "mm-intl"]) == 0
    assert calls == ["https://api.minimaxi.com/v1/chat/completions",
                     "https://api.minimax.io/v1/chat/completions"]
    assert "minimax.io" in capsys.readouterr().out


def test_minimax_base_pins_one_host_and_keeps_the_401_verdict(monkeypatch, capsys):
    _no_remains(monkeypatch)
    calls = []

    def fake(url, headers=None, method="GET", body=None, timeout=25):
        calls.append(url)
        return 401, json.dumps({"error": {"message": "login fail"}})

    monkeypatch.setattr(pc, "fetch", fake)
    assert pc.main(["minimax", "--key", "mm",
                    "--base", "https://example.invalid"]) == 2
    assert calls == ["https://example.invalid/v1/chat/completions"]


def test_no_key_is_exit_3(monkeypatch, capsys):
    monkeypatch.delenv("KIMI_CODING_API_KEY", raising=False)
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    assert pc.main(["all"]) == 3
    assert "no key for kimi" in capsys.readouterr().err


def test_trailing_slash_base_is_tolerated(monkeypatch):
    calls = _monkey(monkeypatch, 200, "{}")
    pc.main(["kimi", "--key", "x", "--base", "https://example.com/"])
    assert calls[0][0] == "https://example.com/v1/usages"

def test_minimax_reads_the_token_plan_quota_from_its_documented_route(monkeypatch, capsys):
    """The route MiniMax documents for this question, not one we guessed."""
    def fake(url, headers=None, method="GET", body=None, timeout=25):
        assert url == "https://www.minimax.cn/v1/token_plan/remains"
        return 200, json.dumps({"remains": 4200, "limit": 6000})

    monkeypatch.setattr(pc, "fetch", fake)
    assert pc.main(["minimax", "--key", "sub-key"]) == 0
    out = capsys.readouterr().out
    assert "token_plan/remains" in out
    assert "4200" in out


def test_a_pay_as_you_go_key_falls_back_to_the_one_token_call(monkeypatch, capsys):
    """A subscription key and a pay-as-you-go key are different things."""
    seen = []

    def fake(url, headers=None, method="GET", body=None, timeout=25):
        seen.append(url)
        if url.endswith("/v1/token_plan/remains"):
            return 200, json.dumps({"base_resp": {"status_code": 1004,
                                             "status_msg": "login fail"}})
        return 200, json.dumps({"choices": [{"message": {"content": "hi"}}]})

    monkeypatch.setattr(pc, "fetch", fake)
    assert pc.main(["minimax", "--key", "payg-key"]) == 0
    assert seen[0].endswith("/v1/token_plan/remains")
    assert seen[-1].endswith("/v1/chat/completions")


def test_a_missing_minimax_key_names_the_credential_it_needs(monkeypatch, capsys):
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    assert pc.main(["minimax"]) == 3
    err = capsys.readouterr().err
    assert "no key for minimax" in err
    assert "subscription Key" in err


def test_a_missing_kimi_key_is_told_where_one_comes_from(monkeypatch, capsys):
    monkeypatch.delenv("KIMI_CODING_API_KEY", raising=False)
    assert pc.main(["kimi", "--no-app-key"]) == 3
    assert "desktop app" in capsys.readouterr().err

"""The credits probe must answer, and must tell a dead key from a dead net."""
import json

import plan_credits as pc


def _monkey(monkeypatch, status, text):
    calls = []

    def fake(url, headers=None, method="GET", body=None, timeout=25):
        calls.append((url, method, body, headers))
        return status, text

    monkeypatch.setattr(pc, "fetch", fake)
    return calls


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


def test_kimi_dead_key_is_exit_2_not_a_crash(monkeypatch, capsys):
    body = json.dumps({"error": {"message": "invalid or expired"}})
    _monkey(monkeypatch, 401, body)
    assert pc.main(["kimi", "--key", "sk-dead"]) == 2
    assert "dead or revoked" in capsys.readouterr().err


def test_minimax_spends_one_token_with_the_bearer(monkeypatch, capsys):
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


def test_no_key_is_exit_3(monkeypatch, capsys):
    monkeypatch.delenv("KIMI_CODING_API_KEY", raising=False)
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    assert pc.main(["all"]) == 3
    assert "no key" in capsys.readouterr().err


def test_trailing_slash_base_is_tolerated(monkeypatch):
    calls = _monkey(monkeypatch, 200, "{}")
    pc.kimi("sk-x", "https://api.kimi.com/coding/")
    assert calls[0][0] == "https://api.kimi.com/coding/v1/usages"

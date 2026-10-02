# Regression tests for code_model_bench: the grader is as testable as the code it grades.
# All of these run offline against built-in fixtures, no network.
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import code_model_bench as b


def _anthropic(text):
    return json.dumps({"content": [{"type": "text", "text": text}]})


def _openai(text):
    return json.dumps({"choices": [{"message": {"content": text}}]})


def test_extract_anthropic_content_blocks():
    payload = "line-a" + chr(10) + "line-b"
    assert b.extract_text(_anthropic(payload)) == payload


def test_extract_openai_shape():
    assert b.extract_text(_openai("plain reply")) == "plain reply"


def test_metric_suffix_is_stripped_before_parse():
    raw = _anthropic("ok") + chr(10) + "|HTTP:200 T:2.5"
    assert b.extract_text(raw) == "ok"


def test_extract_code_fence_roundtrip():
    text = "prose " + chr(10) + "code open " + chr(10) + "```python" + chr(10) + "X = 1" + chr(10) + "```" + chr(10) + " trailer"
    code, matched = b.extract_code(text)
    assert matched is True
    assert code == "X = 1" + chr(10)


def test_grade_good_task1_scores_full_with_zero_prose():
    res = b.grade_response(b.fenced(b.CORRECT_R1), 1)
    assert res["score_str"] == "8/8"
    assert res["prose"] == 0
    assert res["no_runnable"] is False


def test_grade_good_task2_scores_full_with_zero_prose():
    res = b.grade_response(b.fenced(b.CORRECT_R2), 2)
    assert res["score_str"] == "7/7"
    assert res["prose"] == 0
    assert res["no_runnable"] is False


def test_grade_garbage_has_no_runnable_block():
    res = b.grade_response("cannot handle this request", 1)
    assert res["no_runnable"] is True
    assert res["score"] is None


def test_run_offline_self_test_passes():
    ok, rows = b.run_offline()
    assert ok is True
    assert len(rows) == 3

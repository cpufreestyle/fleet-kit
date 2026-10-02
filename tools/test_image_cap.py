"""The image de-duplicator/capper must fit a request under the 70-image ceiling.

Measured 2026-09-30 through CC Switch at 127.0.0.1:15721: 70 images in one
/v1/responses request answer 200, 71 answer HTTP 400 images_too_many, while the
same request sent straight at api.stepfun.com answers 200. Codex re-sends its
history every turn, so the count only grows. These tests pin the rewrite that
keeps it under the ceiling without throwing away anything but duplicate pixels.
"""
import copy
import os
import sys

import pytest

TOOLS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOLS)

import image_cap  # noqa: E402


def _image(tag: str, kind: str = "responses") -> dict:
    url = "data:image/png;base64,iVBORw0KGgo=%s" % tag
    if kind == "responses":
        return {"type": "input_image", "image_url": url}
    return {"type": "image_url", "image_url": {"url": url}}


def _text(kind: str) -> dict:
    return {"type": "text" if kind == "chat" else "input_text",
            "text": "这些图里有什么"}


def _payload(count: int, kind: str = "responses",
             model: str = "stepfun/step-5-preview", distinct: bool = True) -> dict:
    tag = (lambda i: str(i)) if distinct else (lambda i: "same")
    content = [_text(kind)]
    content += [_image(tag(i), kind) for i in range(count)]
    key = "messages" if kind == "chat" else "input"
    return {"model": model, key: [{"role": "user", "content": content}]}
    return {"model": model, "input": [{"role": "user", "content": content}]}


def _image_count(payload: dict) -> int:
    return image_cap.images_in(payload)


def test_identical_images_collapse_to_one():
    payload = _payload(100, distinct=False)
    _, stats = image_cap.cap_images(payload, max_images=32)
    assert stats == {"images": 100, "unique": 1, "kept": 1,
                     "dropped_duplicate": 99, "dropped_cap": 0}
    assert _image_count(payload) == 1


def test_distinct_images_are_capped_keeping_the_newest():
    payload = _payload(100)
    original = copy.deepcopy(payload)
    _, stats = image_cap.cap_images(payload, max_images=32)
    assert stats["unique"] == 100
    assert stats["kept"] == 32
    assert stats["dropped_cap"] == 68
    # the survivors are the last 32, i.e. the most recent screenshots
    survivors = [p for p in payload["input"][0]["content"] if image_cap.is_image_part(p)]
    expected = [p for p in original["input"][0]["content"] if image_cap.is_image_part(p)][-32:]
    assert survivors == expected


def test_request_under_the_cap_is_returned_untouched():
    payload = _payload(5)
    original = copy.deepcopy(payload)
    rewritten, stats = image_cap.cap_images(payload, max_images=32)
    assert rewritten == original
    assert stats["dropped_duplicate"] == 0 and stats["dropped_cap"] == 0
    assert stats["images"] == 5 and stats["kept"] == 5


def test_a_dropped_slot_becomes_a_text_note_not_a_hole():
    payload = _payload(3, kind="chat")
    image_cap.cap_images(payload, max_images=1)
    content = payload["messages"][0]["content"]
    assert len(content) == 4  # 1 text + 3 images, nothing deleted
    notes = [p for p in content if not image_cap.is_image_part(p)]
    assert notes[0]["type"] == "text"          # chat spelling, from the sibling
    assert "超出单请求" in notes[-1]["text"]    # and the note says why


def test_the_response_spelling_of_a_note_follows_its_siblings():
    payload = _payload(3, kind="responses")
    image_cap.cap_images(payload, max_images=1)
    content = payload["input"][0]["content"]
    notes = [p for p in content if not image_cap.is_image_part(p)]
    assert notes[0]["type"] == "input_text"


def test_chat_completions_image_parts_are_counted():
    payload = {"model": "step-3.5-flash",
               "messages": [{"role": "user",
                             "content": [_image(str(i), "chat") for i in range(90)]}]}
    _, stats = image_cap.cap_images(payload, max_images=32)
    assert stats["images"] == 90 and stats["kept"] == 32


def test_string_content_and_non_image_parts_survive():
    payload = {"model": "step-3.5-flash",
               "input": [{"role": "user", "content": "没有图片"},
                         {"role": "assistant", "content": [{"type": "output_text",
                                                           "text": "好的"}]},
                         {"role": "user", "content": [
                             {"type": "input_text", "text": "一张图"},
                             _image("only")]}]}
    original = copy.deepcopy(payload)
    rewritten, stats = image_cap.cap_images(payload, max_images=32)
    assert rewritten == original
    assert stats["images"] == 1 and stats["kept"] == 1


def test_a_duplicate_keeps_the_newest_citation():
    early, late = _image("same"), _image("same")
    payload = {"model": "step-5-preview",
               "input": [{"role": "user",
                          "content": [early, {"type": "input_text", "text": "再看"},
                                      late]}]}
    _, stats = image_cap.cap_images(payload, max_images=32)
    content = payload["input"][0]["content"]
    assert stats["dropped_duplicate"] == 1
    assert content[0] == {"type": "input_text", "text": image_cap.DUPLICATE_NOTE}
    assert content[2] is late


def test_max_images_zero_disables_the_cap_but_still_dedupes():
    payload = _payload(50, distinct=False)
    _, stats = image_cap.cap_images(payload, max_images=0)
    assert stats["images"] == 50 and stats["unique"] == 1
    assert stats["dropped_cap"] == 0 and stats["kept"] == 1


def test_payload_without_images_is_untouched_and_counters_stay_cold():
    payload = {"model": "step-5-preview", "input": "hello"}
    rewritten, stats = image_cap.cap_images(payload, max_images=32)
    assert rewritten == {"model": "step-5-preview", "input": "hello"}
    assert stats["images"] == 0 and stats["unique"] == 0 and stats["kept"] == 0


@pytest.mark.parametrize("kind", ["responses", "chat"])
def test_a_hundred_images_end_up_under_the_measured_ceiling(kind):
    payload = _payload(100, kind=kind)
    image_cap.cap_images(payload, max_images=image_cap.DEFAULT_MAX_IMAGES)
    assert _image_count(payload) <= 70


def test_non_dict_payloads_pass_through():
    for payload in (None, [], "text", 3):
        rewritten, stats = image_cap.cap_images(payload, max_images=32)
        assert rewritten == payload
        assert stats["images"] == 0

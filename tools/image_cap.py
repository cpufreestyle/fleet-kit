"""De-duplicate and cap the images inside one Codex request body.

Measured 2026-09-30 against the StepFun Plan API through CC Switch
(127.0.0.1:15721 -> api.stepfun.com/step_plan/v1, wire_api = responses):

    70 images in one request -> HTTP 200
    71 images in one request -> HTTP 400 images_too_many

The same 71-image request sent straight at api.stepfun.com answers 200, so the
ceiling is not the model: StepFun documents a 1M context window and reads
images happily. It is the CC Switch hop that stops at 70. Codex never holds
that ceiling in mind -- with disable_response_storage = true it re-sends the
whole conversation every turn, so N screenshots pasted by the operator are
resent once per turn, and a long session walks into a 400 the user cannot act
on ("The amount of images you provided exceeds the model's limitation",
addressed to the API caller, not to the person).

Two rewrites make the request fit, both lossless for everything but pixels:

  * de-duplicate -- the same data URL repeated (a re-attached screenshot, a
    tool result echoed twice) is one image, not two. Only the newest citation
    survives, which is where the conversation's current copy lives.
  * cap -- when the de-duplicated total still exceeds the ceiling, keep the
    most recent images and replace the oldest with a short text note. A
    dropped slot becomes text instead of being deleted: an emptied content
    list is a malformed request, and a silent hole reads to the model as "the
    user sent nothing here".

The payload is rewritten in place and returned. Callers hand in a body they
just parsed, so nothing else can observe the mutation.
"""
from __future__ import annotations

import json

# Comfortably under the measured ceiling of 70: enough screenshots for a real
# session, with margin for a slightly tighter upstream limit or one extra image
# added by the client on its way out.
DEFAULT_MAX_IMAGES = 32

# Part types that carry an image. input_image is the Responses API spelling,
# image_url the Chat Completions one, image/source the Anthropic one, and
# output_image covers an image a previous turn produced that the client is
# replaying -- all of them count against the same request-wide ceiling.
IMAGE_PART_TYPES = frozenset(
    {"input_image", "image_url", "image", "output_image"})

CONTAINER_KEYS = ("input", "messages")

TEXT_PART_TYPES = ("input_text", "text", "output_text")

DUPLICATE_NOTE = "[重复图片已省略]"


def image_url_of(part: dict):
    """Return the URL a content part points at, or None when it has none."""
    value = part.get("image_url")
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        inner = value.get("url")
        if isinstance(inner, str):
            return inner
    source = part.get("source")
    if isinstance(source, dict):
        inner = source.get("url")
        if isinstance(inner, str):
            return inner
    url = part.get("url")
    return url if isinstance(url, str) else None


def is_image_part(part) -> bool:
    """True when a content part is an image the ceiling would count."""
    if not isinstance(part, dict):
        return False
    if part.get("type") in IMAGE_PART_TYPES:
        return True
    return isinstance(part.get("image_url"), (str, dict))


def _dedupe_key(part: dict) -> str:
    """Stable identity for one image part.

    The URL when there is one -- two parts sharing a URL are the same picture,
    and for a data URL the URL *is* the image. Without one the part is
    serialised instead, so structurally identical parts still collapse and
    two different ones never do.
    """
    url = image_url_of(part)
    if url:
        return "u:" + url
    return "j:" + json.dumps(part, sort_keys=True, ensure_ascii=False)


def _note_type(content: list) -> str:
    """Text part type to use as a replacement, taken from the siblings.

    Responses wants input_text, Chat Completions wants text, and guessing
    wrong turns a capped image into an invalid request, so the same content
    list decides. input_text is the fallback: that is what this shim serves.
    """
    for part in content:
        if isinstance(part, dict) and part.get("type") in TEXT_PART_TYPES:
            return part["type"]
    return "input_text"


def _replace_with_note(content: list, index: int, note: str) -> None:
    content[index] = {"type": _note_type(content), "text": note}


def _containers(payload: dict):
    """Yield every (items, index) whose item has a part list as its content.

    input/messages hold the conversation and each item's "content" is either a
    string (nothing to cap) or the list of parts this module rewrites. A bare
    message object counts as a one-item container, so a caller sending
    {"role": ..., "content": [...]} is capped too.
    """
    for key in CONTAINER_KEYS:
        items = payload.get(key)
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list):
            continue
        for index, item in enumerate(items):
            if isinstance(item, dict) and isinstance(item.get("content"), list):
                yield items, index


def cap_images(payload, max_images: int = DEFAULT_MAX_IMAGES,
               duplicate_note: str = DUPLICATE_NOTE, cap_note: str = ""):
    """Rewrite payload so it carries at most max_images distinct images.

    Returns (payload, stats). stats counts what happened: images (every image
    part found), unique (after de-duplication), kept, dropped_duplicate and
    dropped_cap. A payload with nothing to do comes back untouched, so the
    caller can tell "no change" from "changed" without diffing bodies.

    max_images <= 0 disables the cap but keeps de-duplication: a repeated
    image is still one image whatever the ceiling is.
    """
    stats = {"images": 0, "unique": 0, "kept": 0,
             "dropped_duplicate": 0, "dropped_cap": 0}
    if not isinstance(payload, dict):
        return payload, stats

    # (content list, index in it, part, dedupe key) in document order.
    slots = []
    for items, index in _containers(payload):
        content = items[index]["content"]
        for part_index, part in enumerate(content):
            if is_image_part(part):
                slots.append((content, part_index, part, _dedupe_key(part)))
    stats["images"] = len(slots)
    if not slots:
        return payload, stats

    # De-duplicate: keep the newest citation of each picture, retire the rest.
    last_seen = {}
    for position, slot in enumerate(slots):
        last_seen[slot[3]] = position
    kept = [slot for position, slot in enumerate(slots)
            if last_seen[slot[3]] == position]
    duplicates = [slot for position, slot in enumerate(slots)
                  if last_seen[slot[3]] != position]
    for content, part_index, _part, _key in duplicates:
        _replace_with_note(content, part_index, duplicate_note)
    stats["dropped_duplicate"] = len(duplicates)
    stats["unique"] = len(kept)

    # Cap: retire the oldest survivors, keep the most recent ones.
    if max_images and max_images > 0 and len(kept) > max_images:
        note = cap_note or ("[图片已省略：超出单请求 %d 张上限]" % max_images)
        for content, part_index, _part, _key in kept[:len(kept) - max_images]:
            _replace_with_note(content, part_index, note)
        stats["dropped_cap"] = len(kept) - max_images
        kept = kept[len(kept) - max_images:]

    stats["kept"] = len(kept)
    return payload, stats


def images_in(payload) -> int:
    """Count the image parts in a payload without rewriting anything."""
    if not isinstance(payload, dict):
        return 0
    total = 0
    for items, index in _containers(payload):
        total += sum(1 for part in items[index]["content"] if is_image_part(part))
    return total

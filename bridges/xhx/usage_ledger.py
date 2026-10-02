"""Local counter for what Codex spends on 小浣熊 models.

measured 2026-09-29: the llm/v2 chat surface this bridge calls returns usage
but never settles 积分. A 528-token call on raccoon-405a1c
(billing_multiplier=1, status normal) left available_points, daily_points and
reward_points untouched, immediately and five minutes later; the official
settlement surface is /api/web/office/v3, which the desktop app uses.

So the only honest usage number this fleet can show is the one counted here.
One JSON line per call, appended under the fleet root, overridable with
XHX_USAGE_FILE:

    {"ts": "...", "model": "raccoon-405a1c", "stream": false, "secs": 8.1,
     "prompt_tokens": 20, "completion_tokens": 508, "total_tokens": 528,
     "reasoning_tokens": 0}

Never raises: a ledger that cannot be written must not take a working bridge
down, and the status panel renders "未启用" instead of a stack trace.
"""
from __future__ import annotations

import datetime
import json
import os

# <fleet root>/xhx-usage.jsonl: three levels up from this file
# (bridges/xhx/usage_ledger.py -> bridges/xhx -> bridges -> fleet root)
DEFAULT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "xhx-usage.jsonl")
MAX_BYTES = 2_000_000      # bound the file before it becomes its own problem
KEEP_LINES = 2_000         # lines kept when trimming
FIELD_TOKENS = ("prompt_tokens", "completion_tokens", "total_tokens",
                "reasoning_tokens")


def ledger_path(path=None) -> str:
    return path or os.environ.get("XHX_USAGE_FILE") or DEFAULT_PATH


def _num(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def record(model, usage=None, *, stream=False, seconds=None, path=None) -> dict:
    """Append one call. Returns the row written (also the value tests assert)."""
    usage = usage if isinstance(usage, dict) else {}
    row = {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "model": str(model or ""),
        "stream": bool(stream),
        "secs": round(float(seconds), 2) if seconds is not None else None,
    }
    for field in FIELD_TOKENS:
        row[field] = _num(usage.get(field))
    target = ledger_path(path)
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        _trim(target)
    except OSError:
        return row
    return row


def load(path=None):
    """Every row, oldest first. A corrupt line is skipped, never fatal."""
    target = ledger_path(path)
    if not os.path.isfile(target):
        return []
    rows = []
    try:
        with open(target, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        return []
    return rows


def summarize(rows=None, day=None, path=None) -> dict:
    """Today's usage by model, plus the share of calls that reported usage."""
    if rows is None:
        rows = load(path)
    day = day or datetime.date.today().isoformat()
    models = {}
    calls = with_usage = 0
    for row in rows:
        if str(row.get("ts") or "")[:10] != day:
            continue
        calls += 1
        model = str(row.get("model") or "?")
        bucket = models.setdefault(model, {"model": model, "calls": 0,
                                           "total_tokens": 0,
                                           "completion_tokens": 0,
                                           "reasoning_tokens": 0,
                                           "last": ""})
        bucket["calls"] += 1
        for field in ("total_tokens", "completion_tokens", "reasoning_tokens"):
            bucket[field] += _num(row.get(field))
        if _num(row.get("total_tokens")):
            with_usage += 1
            bucket["last"] = str(row.get("ts") or "")
        elif str(row.get("ts") or "") > bucket["last"]:
            bucket["last"] = str(row.get("ts") or "")
    per_model = sorted(models.values(), key=lambda item: (-item["total_tokens"],
                                                          -item["calls"], item["model"]))
    return {"day": day, "calls": calls, "with_usage": with_usage,
            "total_tokens": sum(item["total_tokens"] for item in per_model),
            "completion_tokens": sum(item["completion_tokens"] for item in per_model),
            "reasoning_tokens": sum(item["reasoning_tokens"] for item in per_model),
            "models": per_model}


def _trim(target) -> None:
    """Keep the file bounded once it passes MAX_BYTES.

    Newest lines that still fit, never more than KEEP_LINES of them. A byte
    budget rather than a line count: one oversized row must not keep the file
    over the limit forever, and the newest row is always kept even alone.
    """
    try:
        if os.path.getsize(target) <= MAX_BYTES:
            return
        with open(target, encoding="utf-8") as handle:
            lines = handle.readlines()
        kept, size = [], 0
        for line in reversed(lines):
            if len(kept) >= KEEP_LINES or (kept and size + len(line) > MAX_BYTES):
                break
            kept.append(line)
            size += len(line)
        if not kept and lines:
            kept = [lines[-1]]
        kept.reverse()
        tmp = target + ".trim"
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.writelines(kept)
        os.replace(tmp, target)
    except OSError:
        return

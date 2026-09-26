#!/usr/bin/env python3
"""把 Antigravity（Google IDE）模型注入 Codex 的 cc-switch 模型目录（slug: antigravity/<model>）。"""
import json, os, sys, urllib.request
from pathlib import Path

CATALOG = Path(os.path.expanduser("~/.codex/cc-switch-model-catalog.json"))

CTX = {"claude": 200000, "gemini": 1048576}
DEFAULT_CTX = 128000


def ctx_of(mid):
    for key, value in CTX.items():
        if key in mid:
            return value
    return DEFAULT_CTX


def main():
    bridge = os.environ.get("ANTIGRAVITY_BRIDGE", "http://127.0.0.1:8797")
    key = os.environ.get("ANTIGRAVITY2CODEX_KEY", "")
    req = urllib.request.Request(bridge + "/v1/models")
    if key:
        req.add_header("Authorization", "Bearer " + key)
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode())

    models = data.get("data") or []
    if not models:
        print("bridge returned no models")
        return 1

    doc = json.loads(CATALOG.read_text(encoding="utf-8"))
    entries = doc["models"] if isinstance(doc, dict) else doc
    existing = {(m.get("slug") or m.get("id")) for m in entries if isinstance(m, dict)}
    added = 0
    for m in models:
        mid = m["id"].removeprefix("antigravity/")
        slug = "antigravity/" + mid
        if slug in existing:
            continue
        ctx = ctx_of(mid)
        entries.append({
            "slug": slug,
            "display_name": "Antigravity " + mid,
            "supported_reasoning_levels": [
                {"effort": "low", "description": "Fast responses with lighter reasoning"},
                {"effort": "medium", "description": "Balances speed and reasoning depth for everyday tasks"},
                {"effort": "high", "description": "Greater reasoning depth for complex problems"},
                {"effort": "xhigh", "description": "Extra high reasoning depth for complex problems"},
                {"effort": "max", "description": "Maximum reasoning depth for the hardest problems"},
                {"effort": "ultra", "description": "Maximum reasoning with automatic task delegation"}
            ],
            "description": "Google Antigravity IDE 模型 " + mid + "（经 antigravity2codex 本地桥，逆向自 language_server cloudcode-pa）",
            "max_context_window": ctx,
            "context_window": ctx,
            "effective_context_window_percent": 95,
            "default_reasoning_level": "medium",
            "default_reasoning_summary": "auto",
            "support_verbosity": True,
            "input_modalities": ["text", "image"],
            "priority": 50,
        })
        added += 1
    if isinstance(doc, dict):
        doc["models"] = entries
        CATALOG.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        CATALOG.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    print("models from bridge: %d; added %d; catalog total: %d" % (len(models), added, len(entries)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

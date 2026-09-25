#!/usr/bin/env python3
"""把灵犀（LingXi SaaS）模型注入 Codex 的 cc-switch 模型目录（slug: lingxi/<model>）。"""
import json, os, sys, urllib.request
from pathlib import Path

CATALOG = Path(os.path.expanduser("~/.codex/cc-switch-model-catalog.json"))


def main() -> int:
    bridge = os.environ.get("LINGXI_BRIDGE", "http://127.0.0.1:8792")
    key = os.environ.get("LINGXI2CODEX_KEY", "")
    req = urllib.request.Request(f"{bridge}/v1/models")
    if key:
        req.add_header("Authorization", f"Bearer {key}")
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
        mid = m["id"].removeprefix("lingxi/")
        slug = f"lingxi/{mid}"
        if slug in existing:
            continue
        entries.append({
            "slug": slug,
            "display_name": f"LingXi {mid}",
            "supported_reasoning_levels": [
                {"effort": "low", "description": "Fast responses with lighter reasoning"},
                {"effort": "medium", "description": "Balances speed and reasoning depth for everyday tasks"},
                {"effort": "high", "description": "Greater reasoning depth for complex problems"},
                {"effort": "xhigh", "description": "Extra high reasoning depth for complex problems"},
                {"effort": "max", "description": "Maximum reasoning depth for the hardest problems"},
                {"effort": "ultra", "description": "Maximum reasoning with automatic task delegation"}
            ],
            "description": f"灵犀官方 SaaS 订阅模型 {mid}（经 lingxi2codex 本地桥，逆向自 @lingxi-agent/core）",
            "max_context_window": 128000,
            "context_window": 128000,
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
    print(f"models from bridge: {len(models)}; added {added}; catalog total: {len(entries)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

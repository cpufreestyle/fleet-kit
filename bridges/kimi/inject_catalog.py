#!/usr/bin/env python3
"""把 Kimi Code 模型注入 Codex 的 cc-switch 模型目录（slug: kimi/<model>）。"""
import json, os, sys, time
from pathlib import Path

CATALOG = Path(os.path.expanduser("~/.codex/cc-switch-model-catalog.json"))

# 1M 上下文的那两个模型给足窗口，highspeed/256k 档按 256K 记
CONTEXT = {
    "kimi-for-coding": 1048576,
    "kimi-for-coding-highspeed": 262144,
    "k3": 1048576,
    "k3-256k": 262144,
}


def main() -> int:
    bridge = os.environ.get("KIMI_BRIDGE", "http://127.0.0.1:8802")
    key = os.environ.get("KIMI2CODEX_KEY", "")
    import urllib.request
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
        mid = m["id"]
        slug = f"kimi/{mid}"
        if slug in existing:
            continue
        ctx = CONTEXT.get(mid, 262144)
        entries.append({
            "slug": slug,
            "display_name": mid,
            "description": f"Kimi Code 套餐模型 {mid}（经 kimi2codex 本地桥）",
            "max_context_window": ctx,
            "context_window": ctx,
            "effective_context_window_percent": 95,
            "default_reasoning_level": "medium",
            "default_reasoning_summary": "auto",
            "support_verbosity": True,
            "input_modalities": ["text", "image"],
            "priority": 40,
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


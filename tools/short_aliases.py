#!/usr/bin/env python3
"""short_aliases.py - shorten Codex picker names via ocx model/provider aliases.

The Codex picker shows the catalog display_name, which ocx renders as
"<provider-alias>/<model-alias>". Provider-advertised models arrive with long
names (xhx/xhx-sn-sensenova-6-8-flash-lite, workbuddy-gpt/gpt-5.6-luna, ...)
that get cut off in the picker. This script registers short aliases so the
picker shows compact names like xhx/sn-6-8-fl-lite or wbg/gpt-5.6-luna.

Routing is NOT affected: the catalog slug (workbuddy/hy4-preview) is unchanged,
only the human label changes. Aliases live in the opencodex proxy config, so
they survive catalog syncs and reboots.

Talks to the opencodex management API directly (one batched PUT per provider)
and falls back to the ocx CLI when the API is unreachable.

Usage:
  short_aliases.py            apply provider + model aliases, then ocx sync
  short_aliases.py --dry-run  print the mapping without changing anything
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

CATALOG = os.path.expanduser("~/.codex/cc-switch-model-catalog.json")
OCX_HOME = os.path.expanduser("~/.opencodex")
API = "http://127.0.0.1:10100"
PROVIDER_ALIAS = {
    "workbuddy": "wb", "workbuddy-gpt": "wbg", "codely": "cdl",
    "lingxi": "lx", "gemini": "gem", "qoder": "qdr", "tokendance": "tok",
    "catpaw": "cpw",
    "antigravity": "agy",
}
# token replacements applied to the lowercased model id after the bridge-side
# provider prefix is stripped; "" drops the token entirely.
TOKEN_MAP = [
    ("sensenova", ""), ("deepseek", "ds"), ("minimax", "mm"), ("doubao", "db"),
    ("computer_use_subagent", "cu"), ("preview", "pv"), ("official", "off"),
    ("flash", "fl"), ("turbo", "tb"), ("raccoon", "rcn"),
    ("ultraspeed", "us"), ("thinking", "thk"), ("embedding", "emb"),
    ("voiceclone", "vc"), ("voicedesign", "vd"), ("web-search", "web"),
    ("gpt-", ""), ("kimi", "km"), ("gemini", "gem"),
    ("evolving", "evol"), ("seed", "sd"),
    ("longcat", "lc"),
    ("@default", ""), ("@20250929", ""), ("@20251001", ""),
    ("@20251101", ""),
]


def token():
    try:
        with open(os.path.join(OCX_HOME, "admin-api-token"), encoding="utf-8") as fh:
            return fh.read().strip()
    except Exception:
        return ""


def api_put(path, body):
    req = urllib.request.Request(API + path, method="PUT",
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + token()})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status, resp.read().decode("utf-8", "ignore")


def api_get(path):
    req = urllib.request.Request(API + path, method="GET",
                                 headers={"Authorization": "Bearer " + token()})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))


def ocx_cli(args):
    return subprocess.run(["ocx"] + args, capture_output=True, timeout=60)


def live_pairs():
    """[(provider, native-id)] verbatim from the proxy (ids may contain a slash)."""
    pairs = []
    try:
        out = ocx_cli(["models", "live", "--json"])
        data = json.loads(out.stdout.decode("utf-8", "ignore"))
        items = data if isinstance(data, list) else data.get("models", data.get("data", []))
        for item in items:
            provider = item.get("provider") or "?"
            model = str(item.get("id") or "")
            if model:
                pairs.append((provider, model))
    except Exception:
        pass
    return pairs


def catalog_pairs(live):
    """catalog slugs only for providers the proxy does not advertise."""
    live_providers = {provider for provider, _ in live}
    pairs = []
    try:
        data = json.load(open(CATALOG, encoding="utf-8"))
        items = data if isinstance(data, list) else data.get("models", data.get("data", []))
        for item in items:
            slug = item.get("slug") or item.get("id") or ""
            if "/" not in slug:
                continue
            provider, rest = slug.split("/", 1)
            if provider not in live_providers and (provider, rest) not in live:
                pairs.append((provider, rest))
    except Exception:
        pass
    return pairs


def model_pairs():
    """[(provider, native-model-id)] live first, catalog as fallback."""
    pairs, seen = [], set()
    for pair in live_pairs() + catalog_pairs(live_pairs()):
        if pair not in seen:
            seen.add(pair)
            pairs.append(pair)
    return pairs


def prune_stale_aliases(live_pairs):
    """Drop user aliases whose key is a hyphenated form of a live native id.

    Earlier versions keyed aliases by the catalog slug (trae-DeepSeek-V4-Flash)
    while the proxy advertises trae/DeepSeek-V4-Flash; those rows never matched
    a model and block the alias value inside the provider.
    """
    live = set(live_pairs)
    removed = []
    try:
        data = api_get("/api/aliases")
    except Exception:
        return removed
    models = data.get("models") or {}
    for provider, rows in models.items():
        stale = []
        for model, info in rows.items():
            if info.get("source") != "user":
                continue
            if (provider, model) in live:
                continue
            # hyphenated key of a live native id (trae-DeepSeek-V4-Flash vs
            # trae/DeepSeek-V4-Flash): stale and it blocks the alias value
            if (provider, model.replace("-", "/", 1)) in live:
                stale.append(model)
        if stale:
            try:
                api_put("/api/providers/%s/model-aliases" % provider, {"remove": stale})
                removed.extend("%s/%s" % (provider, m) for m in stale)
            except Exception as exc:
                print("  [warn] prune %s: %s" % (provider, str(exc)[:80]))
    return removed


def shorten(provider, model):
    text = model.lower()
    for prefix in (provider.lower() + "-", provider.lower() + "/"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    for old, new in TOKEN_MAP:
        text = text.replace(old, new)
    while "--" in text:
        text = text.replace("--", "-")
    return text.strip("-") or model.lower()


def plan(pairs):
    """[{provider, model, alias, skip}] with collision/uniqueness guards."""
    rows, used = [], {}
    for provider, model in sorted(pairs):
        if provider == "openai":
            continue
        alias = shorten(provider, model)
        if alias in (model.lower(), model):
            # ocx rejects an alias identical to the model id; drop hyphens so
            # the provider alias still reaches the picker (wbg/glm5.2)
            alias = alias.replace("-", "")
            if alias in (model.lower(), model):
                alias = None
        if alias and alias in used.setdefault(provider, set()):
            alias = None  # keep names unique inside a provider
        if alias:
            used[provider].add(alias)
        rows.append({"provider": provider, "model": model, "alias": alias})
    return rows


def apply(rows, dry):
    failures = []
    via_api = bool(token())
    for provider, alias in sorted(PROVIDER_ALIAS.items()):
        if dry:
            continue
        try:
            if via_api:
                api_put("/api/providers/%s/alias" % provider, {"alias": alias})
            else:
                ocx_cli(["alias", "set", provider, alias])
        except Exception as exc:
            failures.append("provider %s: %s" % (provider, str(exc)[:80]))
    by_provider = {}
    for row in rows:
        if row["alias"]:
            by_provider.setdefault(row["provider"], {})[row["model"]] = row["alias"]
    for provider, mapping in sorted(by_provider.items()):
        if dry:
            continue
        try:
            if via_api:
                api_put("/api/providers/%s/model-aliases" % provider, {"set": mapping})
            else:
                for model, alias in mapping.items():
                    ocx_cli(["alias", "set", "%s/%s" % (provider, model), alias])
        except Exception as exc:
            failures.append("models %s: %s" % (provider, str(exc)[:80]))
    return failures


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    rows = plan(model_pairs())
    print("provider aliases:")
    for provider, alias in sorted(PROVIDER_ALIAS.items()):
        print("  %-14s -> %s" % (provider, alias))
    print("model aliases:")
    for row in rows:
        prov_alias = PROVIDER_ALIAS.get(row["provider"], row["provider"])
        target = row["alias"] if row["alias"] else "(keep: %s)" % row["model"]
        print("  %-42s -> %s/%s" % ("%s/%s" % (row["provider"], row["model"]),
                                    prov_alias, target))
    if args.dry_run:
        print("dry-run: nothing changed")
        return 0

    pairs = model_pairs()
    removed = prune_stale_aliases(pairs)
    if removed:
        print("pruned %d stale alias keys: %s" % (len(removed), ", ".join(removed[:6])))
    failures = apply(rows, False)
    out = ocx_cli(["sync"])
    tail = (out.stdout.decode("utf-8", "ignore") or "").strip().splitlines()
    print("ocx sync:", tail[-1] if tail else "rc=%d" % out.returncode)
    print("done: %d models, %d aliased, %d failures"
          % (len(rows), sum(1 for r in rows if r["alias"]), len(failures)))
    for f in failures[:10]:
        print("  [warn]", f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

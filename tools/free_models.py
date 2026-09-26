#!/usr/bin/env python3
"""free_models.py - annotate every fleet model with free-tier status and time windows.

Data source: free-windows.json (repo root) - edit that file to change annotations.
Live models:  ocx models live --json   (falls back to catalog-only when ocx is absent)
Picker state: ~/.codex/cc-switch-model-catalog.json (slug list)

Usage:
  free_models.py                  human table
  free_models.py --json           machine-readable snapshot
  free_models.py --free-only      only free/free-window/quota/trial rows
  free_models.py --provider P     filter one provider
  free_models.py --missing        picker gap report only
  free_models.py --check-sources  probe every official source URL for reachability
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "free-windows.json")
CATALOG = os.path.expanduser("~/.codex/cc-switch-model-catalog.json")
FREE_KINDS = ("free", "free-window", "quota", "trial")
BADGE = {"free": "FREE", "free-window": "LIMITED", "quota": "QUOTA",
         "trial": "TRIAL", "subscription": "SUB", "paid": "PAID",
         "unknown": "N/A", "blocked": "DOWN"}


def load_db():
    with open(DB_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def ocx_live():
    """[(provider, model_id), ...] from the opencodex proxy; [] when unavailable."""
    try:
        out = subprocess.run(["ocx", "models", "live", "--json"],
                             capture_output=True, timeout=30)
        data = json.loads(out.stdout.decode("utf-8", "ignore"))
    except Exception:
        return []
    items = data if isinstance(data, list) else data.get("models", data.get("data", []))
    rows = []
    for item in items:
        provider = item.get("provider") or "?"
        model = str(item.get("id") or "")
        model = model.split("/")[-1] if "/" in model else model
        if model:
            rows.append((provider, model))
    return rows


def catalog_slugs():
    try:
        with open(CATALOG, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return set()
    items = data if isinstance(data, list) else data.get("models", data.get("data", []))
    slugs = set()
    for item in items:
        slug = item.get("slug") or item.get("id") or ""
        if slug:
            slugs.add(slug)
    return slugs


def catalog_index():
    """{slug: display_name} for the Codex picker catalog."""
    index = {}
    try:
        with open(CATALOG, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return index
    items = data if isinstance(data, list) else data.get("models", data.get("data", []))
    for item in items:
        slug = item.get("slug") or item.get("id") or ""
        if slug:
            index[slug] = item.get("display_name") or slug
    return index


def match_slug(index, provider, model):
    """Provider-scoped slug match.

    Catalog slugs look like  provider/model  (some bridges double the prefix,
    e.g. xhx/xhx-sn-...  for model  sn-...).  Native Codex models are bare
    (gpt-5.5).  Cross-provider suffix matches are deliberately rejected so a
    tokendance model can never claim workbuddy/deepseek-v4-flash.
    """
    exact = "%s/%s" % (provider, model)
    if exact in index:
        return exact
    prefix = provider + "/"
    for slug in index:
        if slug.startswith(prefix) and slug[len(prefix):].endswith(model):
            return slug
    if provider == "openai" and "/" not in model and model in index:
        return model
    return None


def annotate(db, provider, model):
    key = "%s/%s" % (provider, model)
    override = db.get("models", {}).get(key)
    pdef = db.get("providers", {}).get(provider, {})
    free = (override or {}).get("free") or pdef.get("free") or "unknown"
    window = (override or {}).get("window") or pdef.get("window") or ""
    source = (override or {}).get("source") or pdef.get("site") or ""
    verified = (override or {}).get("verified", pdef.get("verified", False))
    return {"model": key, "provider": provider, "model_id": model,
            "free": free, "badge": BADGE.get(free, free),
            "window": window, "source": source, "verified": bool(verified)}


def build():
    db = load_db()
    index = catalog_index()
    slugs = set(index)
    live = ocx_live()
    source_kind = "ocx-live"
    seen = set()
    models = []

    def add_row(provider, model, origin):
        if (provider, model) in seen:
            return
        seen.add((provider, model))
        row = annotate(db, provider, model)
        row["picker_slug"] = match_slug(index, provider, model)
        row["in_picker"] = bool(row["picker_slug"])
        # short picker label from ocx aliases (tools/short_aliases.py)
        row["picker_name"] = (index.get(row["picker_slug"]) if row["picker_slug"] else None) or row["model"]
        row["origin"] = origin
        models.append(row)

    for provider, model in live:
        add_row(provider, model, "live")
    if not live:
        source_kind = "catalog-only"
    live_pairs = set(live)
    for slug in sorted(slugs):
        if "/" in slug:
            provider, rest = slug.split("/", 1)
            # catalog slugs keep the bridge-side id; some bridges double the
            # provider prefix (trae/trae-X, xhx/xhx-x) while others genuinely
            # name models codely-air / gemini-2.5-flash.  Only skip when the
            # live list already covers the slug under either spelling.
            if (provider, rest) in live_pairs:
                continue
            doubled = provider.lower() + "-"
            if rest.lower().startswith(doubled) and (provider, rest[len(doubled):]) in live_pairs:
                continue
            add_row(provider, rest, "catalog")
        else:
            add_row("openai", slug, "catalog")
    models.sort(key=lambda r: (r["provider"], r["model_id"]))

    live_by_provider = {}
    for provider, _ in live:
        live_by_provider[provider] = live_by_provider.get(provider, 0) + 1
    picker_by_provider = {}
    for row in models:
        if row["in_picker"]:
            picker_by_provider[row["provider"]] = picker_by_provider.get(row["provider"], 0) + 1
    counts = {}
    for row in models:
        counts[row["free"]] = counts.get(row["free"], 0) + 1
    return {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "db_updated": db.get("updated", "?"),
            "live_source": source_kind,
            "catalog_total": len(slugs),
            "counts": counts,
            "live_by_provider": live_by_provider,
            "picker_by_provider": picker_by_provider,
            "providers": db.get("providers", {}),
            "models": models,
            "gaps": db.get("gaps", []),
            "legend": db.get("legend", {})}


def print_table(snap, free_only=False, provider=None):
    rows = snap["models"]
    if free_only:
        rows = [r for r in rows if r["free"] in FREE_KINDS]
    if provider:
        rows = [r for r in rows if r["provider"] == provider]
    print("free-model annotations (live source: %s, db updated %s, catalog %d entries)"
          % (snap["live_source"], snap["db_updated"], snap["catalog_total"]))
    print("-" * 110)
    for row in rows:
        name = row["picker_name"] if row["in_picker"] else ("%s   [NOT in picker]" % row["model"])
        print("%-26s %-8s %s" % (name, row["badge"], row["window"]))
    print("-" * 110)
    print("counts: " + "  ".join("%s=%d" % (BADGE.get(k, k), v)
                                for k, v in sorted(snap["counts"].items())))
    print("live/in-picker: " + "  ".join(
        "%s %d/%d" % (p, snap["live_by_provider"].get(p, 0),
                      snap["picker_by_provider"].get(p, 0))
        for p in sorted(set(list(snap["live_by_provider"]) + list(snap["picker_by_provider"])))))


def print_missing(snap):
    print("picker gaps (live/catalog models missing from the Codex picker)")
    print("-" * 110)
    missing = [r for r in snap["models"] if not r["in_picker"]]
    for row in missing[:50]:
        print("%-40s %s" % (row["model"], row["window"][:60]))
    if len(missing) > 50:
        print("... and %d more" % (len(missing) - 50))
    print("-" * 110)
    print("provider-level reasons:")
    for gap in snap["gaps"]:
        print("- [%s] %s" % (gap["provider"], gap["reason"]))


def check_sources(snap):
    print("official source reachability:")
    urls = set()
    for pdef in snap["providers"].values():
        for url in pdef.get("sources", []):
            urls.add(url)
    for url in sorted(urls):
        try:
            req = urllib.request.Request(url, method="GET",
                                         headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=12) as resp:
                print("  %-55s HTTP %d" % (url, resp.status))
        except Exception as exc:
            print("  %-55s ERR %s" % (url, str(exc)[:50]))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="annotate fleet models with free status and time windows")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--free-only", action="store_true", help="only free rows")
    parser.add_argument("--provider", help="filter one provider")
    parser.add_argument("--missing", action="store_true", help="picker gap report")
    parser.add_argument("--check-sources", action="store_true", help="probe source URLs")
    args = parser.parse_args(argv)
    snap = build()
    if args.json:
        print(json.dumps(snap, ensure_ascii=False, indent=1))
        return 0
    if args.missing:
        print_missing(snap)
        return 0
    if args.check_sources:
        check_sources(snap)
        return 0
    print_table(snap, free_only=args.free_only, provider=args.provider)
    return 0


if __name__ == "__main__":
    sys.exit(main())

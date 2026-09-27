#!/usr/bin/env python3
"""Reorder the Codex model catalog so reachable providers come first.

The Codex picker renders catalog entries in file order, so a dead bridge
pushes working models off the first screen. This tool sorts the catalog:
providers measured reachable first (in --order order), then unknown ones,
then measured-unreachable ones. --drop-unreachable removes them instead.

Driven by a reachability snapshot:

  {"reachable": ["workbuddy", ...], "unreachable": ["qoder", ...],
   "measured_at": "2026-09-27T17:00:00+08:00"}

Default snapshot: $CODEX_HOME/fleet-reach.json (override with --reach or
FLEET_REACH_FILE). A provider missing from the snapshot is treated as unknown
and stays after the reachable ones, so opt-in providers are never hidden by
accident.

Usage:
  catalog_sort.py            reorder the catalog in place
  catalog_sort.py --dry-run  report the new order, change nothing
"""
import argparse
import json
import os
import shutil
import sys
import time

DEFAULT_ORDER = os.environ.get(
    "FLEET_MODEL_ORDER",
    "workbuddy,workbuddy-gpt,trae,stepfun,xhx,lingxi,cline")


def catalog_path():
    home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    name = "cc-switch-model-catalog.json"
    try:
        with open(os.path.join(home, "config.toml"), encoding="utf-8") as fh:
            for line in fh:
                if line.strip().startswith("model_catalog_json"):
                    value = line.split("=", 1)[1].strip().strip(chr(34)).strip(chr(39))
                    if value:
                        name = value
                    break
    except OSError:
        pass
    return os.path.join(home, name)


def provider_of(slug):
    return slug.split("/", 1)[0] if "/" in slug else None


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
        fh.flush()
        os.fsync(fh.fileno())
    check = json.load(open(tmp, encoding="utf-8"))
    os.replace(tmp, path)
    return len(check.get("models") or [])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reach", default=os.environ.get("FLEET_REACH_FILE", ""))
    ap.add_argument("--catalog", default=None)
    ap.add_argument("--order", default=DEFAULT_ORDER)
    ap.add_argument("--drop-unreachable", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-backup", action="store_true")
    args = ap.parse_args()

    reach_file = args.reach or os.path.join(
        os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"),
        "fleet-reach.json")

    reach = {}
    if os.path.exists(reach_file):
        with open(reach_file, encoding="utf-8") as fh:
            reach = json.load(fh)
    good = set(reach.get("reachable") or [])
    bad = set(reach.get("unreachable") or [])
    order = [p.strip() for p in args.order.split(",") if p.strip()]

    path = args.catalog or catalog_path()
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    models = data.get("models") or []

    def rank(model):
        slug = model.get("slug") or model.get("id") or ""
        prov = provider_of(slug)
        if prov in good:
            tier = 0
        elif prov in bad:
            tier = 2
        else:
            tier = 1
        pos = order.index(prov) if prov in order else len(order)
        return (tier, pos, slug)

    kept, dropped = [], {}
    for model in sorted(models, key=rank):
        slug = model.get("slug") or model.get("id") or ""
        prov = provider_of(slug)
        if args.drop_unreachable and prov in bad:
            dropped.setdefault(prov, []).append(slug)
        else:
            kept.append(model)

    summary = {
        "catalog": path,
        "before": len(models),
        "after": len(kept),
        "reach_file": reach_file,
        "measured_at": reach.get("measured_at"),
        "reachable": sorted(good),
        "unreachable": sorted(bad),
        "dropped_by_provider": {k: len(v) for k, v in sorted(dropped.items())},
        "first20": [m.get("slug") or m.get("id") for m in kept[:20]],
    }

    before_slugs = [m.get("slug") for m in models]
    after_slugs = [m.get("slug") for m in kept]
    changed = before_slugs != after_slugs
    if args.dry_run:
        summary["dry_run"] = True
    elif not changed and not dropped:
        summary["note"] = "already in order"
    else:
        if not args.no_backup:
            bak = path + time.strftime(".bak-%Y%m%d-%H%M%S")
            shutil.copy2(path, bak)
            summary["backup"] = bak
        data["models"] = kept
        summary["written"] = write_json(path, data)

    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())

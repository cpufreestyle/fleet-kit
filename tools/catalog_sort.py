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

The picker also sorts on each entry's "priority" field, so reordering the
list alone is not enough: this tool rewrites priority as well. Native
Codex rows keep their 105 marker; fleet rows get 0..N by reachability rank.
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
    ap.add_argument("--strict-coverage", action="store_true",
                    help="fail instead of warn when the snapshot misses providers")
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
    # A snapshot with no measurable verdict would silently promote every
    # provider back to "unknown", i.e. back to alphabetical noise. Refuse
    # rather than destroy a working ordering on a truncated probe run.
    if reach and not reach.get("reachable") and not reach.get("unreachable"):
        print("reach snapshot has no verdicts; refusing to reorder",
              file=sys.stderr)
        return 2
    good = set(reach.get("reachable") or [])
    bad = set(reach.get("unreachable") or [])
    verified = reach.get("verified_models") or {}
    order = [p.strip() for p in args.order.split(",") if p.strip()]

    path = args.catalog or catalog_path()
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    models = data.get("models") or []
    # A truncated probe run (killed mid-sweep) leaves most bridges unmeasured,
    # and those silently become "unknown" and sink below known-good rows.
    # Require the snapshot to cover the providers actually in the catalog.
    catalog_providers = {provider_of(m.get("slug") or m.get("id") or "")
                         for m in (data.get("models") or [])}
    catalog_providers.discard(None)
    covered = good | bad
    missing = sorted(catalog_providers - covered)
    if missing:
        summary_note = ("snapshot covers %d/%d catalog providers; missing: %s"
                        % (len(covered & catalog_providers),
                           len(catalog_providers), ", ".join(missing)))
        if args.strict_coverage:
            print("fleet-sort: " + summary_note, file=sys.stderr)
            return 4
        print("fleet-sort: WARNING " + summary_note, file=sys.stderr)

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
        proven = 0 if verified.get(prov) == slug else 1
        return (tier, proven, pos, slug)

    NATIVE_PRIORITY = 105

    def priority_for(slug, tier, pos):
        prov = provider_of(slug)
        if prov is None:
            return NATIVE_PRIORITY
        return min(tier, 4) * 100 + pos

    kept, dropped = [], {}
    original = [{k: (dict(v) if isinstance(v, dict) else v)
                 for k, v in m.items()} for m in models]
    for model in sorted(models, key=rank):
        slug = model.get("slug") or model.get("id") or ""
        prov = provider_of(slug)
        tier = 0 if prov in good else (2 if prov in bad else 1)
        pos = order.index(prov) if prov in order else len(order)
        bare = slug.split("/", 1)[1] if "/" in slug else slug
        proven = 0 if verified.get(prov) in (slug, bare) else 1
        model["priority"] = priority_for(slug, tier, pos) + proven
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

    # The picker sorts on priority, so prove reachable rows really sort first.
    good_idx = [i for i, m in enumerate(kept)
                if provider_of(m.get("slug") or m.get("id") or "") in good]
    bad_idx = [i for i, m in enumerate(kept)
               if provider_of(m.get("slug") or m.get("id") or "") in bad]
    good_pri = [m["priority"] for m in kept
                if provider_of(m.get("slug") or m.get("id") or "") in good
                and provider_of(m.get("slug") or m.get("id") or "") is not None]
    bad_pri = [m["priority"] for m in kept
               if provider_of(m.get("slug") or m.get("id") or "") in bad]
    by_priority = sorted(kept, key=lambda m: m["priority"])
    summary["priority_rewritten"] = True
    summary["order_ok"] = bool(
        not good_idx or not bad_idx or max(good_idx) < min(bad_idx))
    summary["priority_ok"] = bool(
        not good_pri or not bad_pri or max(good_pri) < min(bad_pri))
    summary["first_by_priority"] = [
        m.get("slug") or m.get("id") for m in by_priority[:8]]

    before_slugs = [m.get("slug") for m in models]
    after_slugs = [m.get("slug") for m in kept]
    before_prio = [m.get("priority") for m in models]
    after_prio = [m.get("priority") for m in kept]
    before_slugs = [m.get("slug") for m in original]
    before_prio = [m.get("priority") for m in original]
    changed = before_slugs != after_slugs or before_prio != after_prio
    # the picker reads models_cache.json too, so a stale cache counts as a change
    cache = os.path.join(os.path.dirname(path), "models_cache.json")
    if cache != path and os.path.exists(cache):
        try:
            cmods = json.load(open(cache, encoding="utf-8")).get("models") or []
            cprio = [m.get("priority") for m in cmods]
            cslugs = [m.get("slug") for m in cmods]
            idx = {m.get("slug"): m for m in kept}
            want = [idx.get(s, {}).get("priority") for s in cslugs]
            if cprio != want:
                changed = True
        except Exception:
            pass
    if args.dry_run:
        summary["dry_run"] = True
    elif not changed and not dropped:
        summary["note"] = "already in order"
    elif not summary["order_ok"] or not summary["priority_ok"]:
        print(json.dumps(summary, ensure_ascii=False, indent=1))
        print("refusing: reachable rows do not sort ahead of unreachable ones",
              file=sys.stderr)
        return 5
    else:
        if not args.no_backup:
            bak = path + time.strftime(".bak-%Y%m%d-%H%M%S")
            shutil.copy2(path, bak)
            summary["backup"] = bak
        data["models"] = kept
        summary["written"] = write_json(path, data)
        # Codex reads models_cache.json, not just the catalog config points at
        cache = os.path.join(os.path.dirname(path), "models_cache.json")
        if os.path.exists(cache) and cache != path:
            try:
                cdata = json.load(open(cache, encoding="utf-8"))
                cmodels = cdata.get("models") or []
                if cmodels:
                    idx = {m.get("slug"): m for m in kept}
                    cdata["models"] = [idx.get(x.get("slug"), x) for x in
                                        sorted(cmodels, key=lambda y: idx.get(
                                            y.get("slug"), {}).get(
                                            "priority", 10**9))]
                    write_json(cache, cdata)
                    summary["cache_written"] = len(cdata["models"])
            except Exception as exc:
                summary["cache_error"] = str(exc)[:80]

    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())

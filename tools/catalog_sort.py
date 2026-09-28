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
    "tokendance,trae,cline,workbuddy-gpt,workbuddy,stepfun,catpaw,xhx,codely,"
    "gemini,qoder,lingxi,antigravity,zcode,qwen")

# Model families, in the order the user wants them listed. A family is a
# vendor substring matched against the model id, so zcode/GLM-5.3 and
# tokendance/glm-5.3 land in the same bucket. Rows matching no family sort
# after every family, so opt-in models are never hidden by accident.
FAMILY_ORDER = tuple(
    f.strip().lower() for f in os.environ.get(
        "FLEET_MODEL_FAMILIES", "deepseek,glm,step,seed").split(",") if f.strip())


def family_of(slug, families=FAMILY_ORDER):
    """Which model family a row belongs to, or None."""
    name = (slug or "").split("/", 1)[-1].lower()
    for family in FAMILY_ORDER:
        if family in name:
            return family
    return None


def family_rank(slug, families=FAMILY_ORDER):
    """Sort key for the family half of the ordering."""
    family = family_of(slug, families)
    if family is None:
        return len(families)
    return families.index(family)

# hy4 is the model the user asks to see first, even when the bridge behind
# it is momentarily unreachable. Everything else still obeys tiering.
HY4_SLUGS = ("workbuddy/hy4-preview", "workbuddy-gpt/hy4-preview")


def is_hy4(slug):
    return slug in HY4_SLUGS

def interleave_reps(models, order, good=None, families=()):
    """Float one representative per family, then per provider, to the front.

    Sorting by tier alone lets one big provider swallow the whole first
    screen: workbuddy alone has a dozen rows, so every other reachable
    provider got pushed past position 30. Leading with one row per
    provider keeps the reachable set visible at the top, then each
    provider keeps its block in order.

    When good is provided, only reachable providers get a front-row
    representative. Unreachable providers stay in their tier block.

    families lists model vendors (deepseek, glm, step, seed). One row per
    family leads, so the opening screen spans the families instead of
    sitting inside a single vendor's block; --order then does the same for
    providers.
    """
    reps, rest = [], []
    taken = set()
    pool = [m for m in models
            if good is None or provider_of(slug_of(m)) in good]
    # One row per model family first, so the picker's opening screen shows
    # deepseek, glm, step and seed side by side instead of one vendor's
    # whole block.
    if families:
        for family in families:
            for model in pool:
                if family_of(slug_of(model), families) == family \
                        and slug_of(model) not in taken:
                    reps.append(model)
                    taken.add(slug_of(model))
                    break
    for prov in order:
        if good is not None and prov not in good:
            continue
        for model in pool:
            slug = model.get("slug") or model.get("id") or ""
            if provider_of(slug) == prov and slug not in taken:
                reps.append(model)
                taken.add(slug)
                break
    for model in models:
        slug = model.get("slug") or model.get("id") or ""
        if slug not in taken:
            rest.append(model)
            taken.add(slug)
    return reps + rest


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


def slug_of(model):
    return model.get("slug") or model.get("id") or ""


def proven_candidates(verified_model, provider):
    """Slugs matching the model the prober verified.

    The prober reports what a bridge hands back, and that namespacing differs
    from the catalog in three ways:
      * a bridge repeats its own prefix (trae/trae/Doubao-X vs trae/Doubao-X)
      * a vendor prefix becomes a dash (cohere/north-mini:free vs
        cohere-north-mini:free)
      * a local bridge reports the bare id (glm-5.2)
    """
    if not verified_model:
        return frozenset()
    out = {verified_model}
    if "/" in verified_model:
        vendor, rest = verified_model.split("/", 1)
        out.update({rest, vendor + "-" + rest,
                    provider + "-" + rest, provider + "/" + rest})
        # the catalog re-namespaces as <provider>/<vendor>-<rest>
        out.add(provider + "/" + vendor + "-" + rest)
    else:
        # a local bridge reports the bare id; catalog is <provider>/<id>
        out.add(provider + "/" + verified_model)
    return frozenset(out)


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
    ap.add_argument("--families", default=",".join(FAMILY_ORDER),
                    help="model families listed first, in this order "
                         "(default: %s)" % ",".join(FAMILY_ORDER))
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
        try:
            with open(reach_file, encoding="utf-8") as fh:
                reach = json.load(fh)
        except (OSError, ValueError) as exc:
            print("fleet-sort: reach snapshot unreadable: %s" % exc,
                  file=sys.stderr)
            return 2
    else:
        print("fleet-sort: no reach snapshot at %s; refusing to reorder"
              % reach_file, file=sys.stderr)
        return 2
    if not isinstance(reach, dict):
        print("fleet-sort: reach snapshot is %s, expected an object"
              % type(reach).__name__, file=sys.stderr)
        return 2

    # A snapshot that simply is not there is not a verdict: it would sink
    # every provider to "unknown" and reshuffle the whole picker.
    if reach and os.path.exists(reach_file) and not (reach.get("reachable")
                                                    or reach.get("unreachable")):
        print("fleet-sort: reach snapshot has no verdicts; refusing to reorder",
              file=sys.stderr)
        return 2
    for field, kind in (("reachable", list), ("unreachable", list),
                        ("verified_models", dict)):
        value = reach.get(field)
        if value is not None and not isinstance(value, kind):
            print("fleet-sort: reach field %r is %s, expected %s"
                  % (field, type(value).__name__, kind.__name__),
                  file=sys.stderr)
            return 2
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
    families = tuple(f.strip().lower() for f in args.families.split(",")
                     if f.strip())

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
        proven = 0 if slug in proven_candidates(verified.get(prov), prov) else 1
        # families come before provenance: the user asked for deepseek, glm,
        # step and seed blocks, so a family must not be split by a bogus
        # probe result on another vendor's bridge
        fam = family_rank(slug, families)
        # workbuddy hy4 is the model the user asks to see first
        hy4 = 0 if is_hy4(slug) else 1
        return (tier, hy4, fam, proven, pos, slug)

    NATIVE_PRIORITY = 105

    def priority_for(slug, tier, pos):
        prov = provider_of(slug)
        if prov is None:
            return NATIVE_PRIORITY
        return min(tier, 4) * 100 + pos

    kept, dropped = [], {}
    original = [{k: (dict(v) if isinstance(v, dict) else v)
                 for k, v in m.items()} for m in models]
    # one representative per family and per provider first, then each
    # provider keeps its block: otherwise the biggest provider, or the
    # biggest vendor, eats the first screen
    ordered = interleave_reps(sorted(models, key=rank), order, good=good,
                             families=families)
    for _rank_i, model in enumerate(ordered):
        slug = model.get("slug") or model.get("id") or ""
        prov = provider_of(slug)
        tier = 0 if prov in good else (2 if prov in bad else 1)
        pos = order.index(prov) if prov in order else len(order)
        proven = 0 if slug in proven_candidates(verified.get(prov), prov) else 1
        model["priority"] = _rank_i * 1000 + priority_for(slug, tier, pos) + proven
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
    slug_of = lambda m: m.get("slug") or m.get("id") or ""
    # hy4 rows stay out of both sets: they are pinned above their own tier on
    # purpose, so counting them would fail every sort while gpt is down.
    good_idx = [i for i, m in enumerate(kept)
                if provider_of(slug_of(m)) in good and not is_hy4(slug_of(m))]
    bad_idx = [i for i, m in enumerate(kept)
               if provider_of(slug_of(m)) in bad and not is_hy4(slug_of(m))]
    good_pri = [m["priority"] for m in kept
                if provider_of(slug_of(m)) in good
                and provider_of(slug_of(m)) is not None
                and not is_hy4(slug_of(m))]
    bad_pri = [m["priority"] for m in kept
               if provider_of(slug_of(m)) in bad and not is_hy4(slug_of(m))]
    by_priority = sorted(kept, key=lambda m: m["priority"])
    summary["priority_rewritten"] = True
    summary["order_ok"] = bool(
        not good_idx or not bad_idx or max(good_idx) < min(bad_idx))
    summary["priority_ok"] = bool(
        not good_pri or not bad_pri or max(good_pri) < min(bad_pri))
    summary["first_by_priority"] = [
        m.get("slug") or m.get("id") for m in by_priority[:8]]
    summary["families"] = list(families)
    summary["family_heads"] = [f for f in families
                               if any(family_of(slug_of(m)) == f for m in kept)]

    before_slugs = [m.get("slug") for m in models]
    after_slugs = [m.get("slug") for m in kept]
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

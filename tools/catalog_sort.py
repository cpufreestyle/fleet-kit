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

--drop-unreachable hides rows from providers the snapshot calls dead, but
only while that snapshot is fresh (--max-reach-age, default 24h). An older
snapshot still orders the rows; it just stops being allowed to delete them,
because bridges recover and a stale verdict would hide working models.

Deletion is also limited to providers no bridge serves at all. A provider
with a bridge gets re-probed on every refresh and can come back on its own,
and a single upstream 503 is enough to mark one dead for a whole cycle, so
the probe alone is not a safe authority to erase a row the status panel may
still be verifying as REAL. Those rows are only reordered, never dropped.

The picker also sorts on each entry's "priority" field, so reordering the
list alone is not enough: this tool rewrites priority as well. Native
Codex rows keep their 105 marker; fleet rows get 0..N by reachability rank.

Each write drops a .bak-<timestamp> next to the catalog and, when rows are
mirrored into models_cache.json, next to that cache as well. Older backups
are pruned down to --keep-backups (default 5), since the 5 minute timers
would otherwise accumulate them without bound.
"""
import argparse
import datetime
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



# Per-provider "important models" — listed first inside each provider block,
# in the order given here. Matches are checked against the model slug part
# (after the provider prefix), case-insensitive. Anything not listed falls
# back to family + version-number ordering.
PER_PROVIDER_IMPORTANT = {
    "workbuddy": ("hy4-preview", "hy3", "deepseek-v4-pro", "deepseek-v4-flash",
                  "glm-5.3", "glm-5.2"),
    "workbuddy-gpt": ("hy4-preview", "gpt-5.6-luna", "gpt-5.5", "gpt-5.4",
                       "glm-5.3", "gemini-3.5-flash"),
    "stepfun": ("step-5-preview", "step-3.7-flash", "step-3.5-flash",
                 "step-router-v1"),
    "trae": ("seed-code-pro-0430", "kimi-k2.7-code", "Doubao-Seed-2.0-Code",
              "Doubao-Seed-Evolving", "Doubao-Seed-2.1-Pro", "kimi-k3",
              "DeepSeek-V4-Pro", "DeepSeek-V4-Flash"),
    "cline": ("cline-free-deepseek-v4.1-flash", "z-ai-glm-5.3-flash",
               "cline-free-muse-spark-1.3-contributor"),
    "qoder": ("GLM-5.3", "GLM-5.3-Flash", "GLM-5.2", "DeepSeek-Flash",
               "Kimi-K3"),
    "xhx": ("xhx-sn-deepseek-v4-1-flash", "xhx-sn-glm-5-3",
             "xhx-sn-glm-5-3-flash", "xhx-sn-kimi-k3"),
    "codely": ("codely-core", "codely-air", "codely-flash", "codely-basic",
                "codely-vl"),
    "lingxi": ("lingxi-deepseek-flash", "lingxi-glm-5.3-flash"),
    "zcode": ("GLM-5.3", "GLM-5.3-Flash"),
    "catpaw": ("glm-5.3-flashx", "glm-5.2", "glm-5.1", "deepseek-v3.2"),
    "gemini": ("gemini-3-pro-preview", "gemini-3-flash-preview",
                "gemini-2.5-pro", "gemini-2.5-flash"),
    "antigravity": ("claude-opus-4-8@default", "claude-opus-4-6@default",
                     "claude-opus-4-5@20251101", "claude-sonnet-4-5@20250929",
                     "gemini-3-flash-preview"),
    "tokendance": ("deepseek-v4.1-flash", "deepseek-v4-pro", "glm-5.3",
                     "glm-5.2", "qwen-3.7-plus"),
    "qwen": ("qwen3.8-max", "qwen-3.7-plus"),
}


def important_rank(slug):
    """Position inside the provider's important list, or len(list) if absent.

    Matching is substring-based against the lowercased model name (the
    part after the provider prefix). This handles bridges that repeat
    their own prefix (trae/trae-Doubao vs trae/Doubao) and other minor
    naming differences. First match wins, so order in the list matters
    and more-specific needles should come first.
    """
    prov = provider_of(slug)
    name = (slug.split("/", 1)[1] if "/" in slug else slug).lower()
    lst = PER_PROVIDER_IMPORTANT.get(prov, ())
    for i, needle in enumerate(lst):
        if needle.lower() in name:
            return i
    return len(lst)

def _version_key(slug):
    """Descending version-number key: higher versions sort first."""
    import re as _re
    nums = [int(n) for n in _re.findall(r"\d+", slug)]
    return tuple(-n for n in nums) or (0,)


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
    """Float one representative per reachable provider to the front.

    With client-first ordering, each provider's models form a contiguous
    block. Without reps, the biggest provider (e.g. tokendance with 96
    models) would push every other provider off the first screen. Leading
    with one row per reachable provider keeps the whole fleet visible at
    the top; the full blocks follow in order.

    The representative is the most important model in that provider per
    PER_PROVIDER_IMPORTANT. workbuddy hy4 is always the very first row,
    because the user wants it pinned first.
    """
    reps, rest = [], []
    taken = set()
    pool = [m for m in models
            if good is None or provider_of(slug_of(m)) in good]
    # hy4 first — always, even before the other reps, so the user's go-to
    # model is the first row in the picker.
    for model in pool:
        if is_hy4(slug_of(model)):
            reps.append(model)
            taken.add(slug_of(model))
            break
    # One row per reachable provider, in --order order, skipping hy4 since
    # it already leads. Picking the "most important" model per provider
    # gives the user a quick scan of the whole fleet on the first screen.
    for prov in order:
        if good is not None and prov not in good:
            continue
        best = None
        best_rank = None
        for model in pool:
            slug = slug_of(model)
            if provider_of(slug) != prov or slug in taken:
                continue
            r = important_rank(slug)
            if best is None or r < best_rank:
                best = model
                best_rank = r
        if best is not None:
            reps.append(best)
            taken.add(slug_of(best))
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


def bridged_providers():
    """Providers a bridge can answer for, from the shared platform table.

    Returns None when the table cannot be read, and the caller then treats
    every provider as bridged: refusing to delete is the safe failure here.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    try:
        from fleet_platform import PORT_OFFSETS
        return set(PORT_OFFSETS)
    except Exception:
        return None


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


def snapshot_age_seconds(reach):
    """Age of a reach snapshot in seconds, or None when unmeasurable.

    A snapshot with no readable measured_at is treated as unmeasurable, and
    the caller decides what that means for deletion.
    """
    stamp = reach.get("measured_at")
    if not stamp:
        return None
    try:
        when = datetime.datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    return max(0.0, (datetime.datetime.now(when.tzinfo) - when).total_seconds())


def prune_backups(path, keep):
    """Keep the newest `keep` .bak-<timestamp> files beside the catalog.

    The 5 minute timers rewrite the catalog all day, and each write leaves a
    timestamped backup. Without a bound those pile up forever (89 files,
    99MB in one measured case), so trim to the newest few after a write.
    """
    if keep < 0:
        return []
    directory = os.path.dirname(path) or "."
    base = os.path.basename(path) + ".bak-"
    found = []
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    for name in names:
        if name.startswith(base):
            full = os.path.join(directory, name)
            try:
                found.append((os.path.getmtime(full), full))
            except OSError:
                continue
    found.sort(reverse=True)
    removed = []
    for _mtime, full in found[keep:]:
        try:
            os.unlink(full)
            removed.append(full)
        except OSError:
            continue
    return removed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reach", default=os.environ.get("FLEET_REACH_FILE", ""))
    ap.add_argument("--catalog", default=None)
    ap.add_argument("--order", default=DEFAULT_ORDER)
    ap.add_argument("--families", default=",".join(FAMILY_ORDER),
                    help="model families listed first, in this order "
                         "(default: %s)" % ",".join(FAMILY_ORDER))
    ap.add_argument("--drop-unreachable", action="store_true")
    ap.add_argument("--max-reach-age", type=float,
                    default=float(os.environ.get("FLEET_REACH_MAX_AGE", 86400)),
                    help="stale after this many seconds the snapshot may order "
                         "rows but not delete them (default: 86400)")
    ap.add_argument("--keep-backups", type=int,
                    default=int(os.environ.get("FLEET_KEEP_BACKUPS", 5)),
                    help="how many .bak files to keep beside the catalog "
                         "(default: 5, negative keeps all)")
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

    # An "unreachable" verdict is a claim about a moment, and the sweep only
    # runs every 30 minutes. Past the freshness window the claim stops being
    # evidence, and ordering on it buries a provider that may have recovered
    # days ago -- zcode sat at row 67 of 117 on a four-day-old 503. The rows
    # are not promoted to reachable, nothing here re-measured them, but they
    # stop sinking below every unknown provider. This is the same rule the
    # drop path below already follows, applied to ordering as well.
    reach_age = snapshot_age_seconds(reach)
    sinking = set(bad)
    stale_verdicts = []
    if reach_age is not None and reach_age > args.max_reach_age:
        sinking = set()
        stale_verdicts = sorted(bad)
        print("fleet-sort: not sinking stale unreachable verdicts: snapshot "
              "is %.1fh old (limit %.1fh): %s"
              % (reach_age / 3600.0, args.max_reach_age / 3600.0,
                 ", ".join(stale_verdicts)), file=sys.stderr)

    # Deletion is the one irreversible action here, so it needs a verdict
    # that is still current. A stale snapshot keeps ordering rows (harmless)
    # but stops deleting them: bridges recover, and the probe timer only runs
    # every 30 minutes, so an aged "unreachable" is not trustworthy enough to
    # erase a provider the user may be using.
    drop_unreachable = args.drop_unreachable
    drop_providers = set()
    bridged = bridged_providers()
    if drop_unreachable:
        if reach_age is None:
            drop_unreachable = False
            reason = "snapshot has no readable measured_at"
        elif reach_age > args.max_reach_age:
            drop_unreachable = False
            reason = ("snapshot is %.1fh old (limit %.1fh)"
                      % (reach_age / 3600.0, args.max_reach_age / 3600.0))
        else:
            reason = None
        if reason:
            print("fleet-sort: not dropping unreachable rows: %s" % reason,
                  file=sys.stderr)
        if bridged is None:
            print("fleet-sort: cannot read the bridge table; dropping nothing",
                  file=sys.stderr)
            drop_unreachable = False
        else:
            # A bridged provider is re-probed every cycle and the panel can
            # still call it REAL, so a single failed probe is not authority to
            # erase its rows. Only bridge-less providers (dead keys nobody can
            # re-verify) are dropped.
            drop_providers = bad - bridged
            reorder_only = sorted(bad & bridged)
            if reorder_only:
                print("fleet-sort: only reordering (not dropping) bridged "
                      "providers the probe called dead: %s"
                      % ", ".join(reorder_only), file=sys.stderr)
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
    # A provider the probe deliberately skips (zcode needs a per-call captcha)
    # is accounted for, not unmeasured: without this every strict sort refuses
    # forever on that one bridge and the ordering silently stops being applied.
    skipped = set((reach.get("skipped") or {}).keys())
    covered = good | bad | skipped
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
        elif prov in sinking:
            tier = 2
        else:
            tier = 1
        # Provider (client) order is the primary sort key inside each tier,
        # so every model from provider X stays together in a single block.
        pos = order.index(prov) if prov in order else len(order)
        proven = 0 if slug in proven_candidates(verified.get(prov), prov) else 1
        # workbuddy hy4 is pinned first (even before the per-provider list)
        hy4 = 0 if is_hy4(slug) else 1
        # Per-provider important models come next, in the listed order.
        imp = important_rank(slug)
        # Then fall back to family order and version-number (desc),
        # so pro > flash > older versions within the same family.
        fam = family_rank(slug, families)
        ver = _version_key(slug)
        return (tier, pos, hy4, imp, proven, fam, ver, slug)

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
        tier = 0 if prov in good else (2 if prov in sinking else 1)
        pos = order.index(prov) if prov in order else len(order)
        proven = 0 if slug in proven_candidates(verified.get(prov), prov) else 1
        model["priority"] = _rank_i * 1000 + priority_for(slug, tier, pos) + proven
        if drop_unreachable and prov in drop_providers:
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
        "stale_verdicts_not_sunk": stale_verdicts,
        "reach_age_seconds": None if reach_age is None else round(reach_age),
        "dropped_enabled": drop_unreachable,
        "skipped_providers": sorted(skipped & catalog_providers),
        "dropped_providers": sorted(drop_providers),
        "reordered_not_dropped": sorted(bad & (bridged or set())),
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
               if provider_of(slug_of(m)) in sinking and not is_hy4(slug_of(m))]
    good_pri = [m["priority"] for m in kept
                if provider_of(slug_of(m)) in good
                and provider_of(slug_of(m)) is not None
                and not is_hy4(slug_of(m))]
    bad_pri = [m["priority"] for m in kept
               if provider_of(slug_of(m)) in sinking and not is_hy4(slug_of(m))]
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

    after_slugs = [m.get("slug") for m in kept]
    after_prio = [m.get("priority") for m in kept]
    before_slugs = [m.get("slug") for m in original]
    before_prio = [m.get("priority") for m in original]
    changed = before_slugs != after_slugs or before_prio != after_prio
    # The picker reads models_cache.json as well as the catalog, so a row
    # dropped from one but left in the other stays visible: treat a cache that
    # disagrees on the row set or the order as a change, and mirror both.
    cache = os.path.join(os.path.dirname(path), "models_cache.json")
    cache_models = None
    cache_before = 0
    if cache != path and os.path.exists(cache):
        try:
            cache_models = json.load(open(cache, encoding="utf-8"))
            cmods = cache_models.get("models") or []
            cache_before = len(cmods)
            # Compare slugs, not priorities: priorities travel with their row,
            # so a cache holding the right rows in the wrong order still has
            # matching priorities and would read as "already in order".
            want_slugs = [m.get("slug") for m in kept]
            if [m.get("slug") for m in cmods] != want_slugs:
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
        pruned = prune_backups(path, args.keep_backups)
        if pruned:
            summary["backups_pruned"] = [os.path.basename(p) for p in pruned]
        if cache_models is not None and cache != path:
            try:
                idx = {m.get("slug"): m for m in kept}
                # Drop rows the catalog no longer has, refresh the survivors,
                # then order exactly like the catalog: the cache alone is not
                # proof a row was removed, so this is what actually hides it.
                # Codex writes this second file too, so it gets the same
                # rescue copy and the same bound on its backups.
                if not args.no_backup:
                    cache_bak = cache + time.strftime(".bak-%Y%m%d-%H%M%S")
                    shutil.copy2(cache, cache_bak)
                    summary["cache_backup"] = cache_bak
                cache_models["models"] = [idx[m.get("slug")] for m in kept
                                          if m.get("slug") in idx]
                write_json(cache, cache_models)
                summary["cache_written"] = len(cache_models["models"])
                summary["cache_dropped"] = cache_before - len(
                    cache_models["models"])
                pruned = prune_backups(cache, args.keep_backups)
                if pruned:
                    summary["cache_backups_pruned"] = [
                        os.path.basename(p) for p in pruned]
            except Exception as exc:
                summary["cache_error"] = str(exc)[:80]

    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())

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
--pin FILE restricts the catalog to a whitelist of slugs. An ocx sync
rediscovers models from whatever keys are configured, and a dead or
half-provisioned key can add dozens of junk rows in one pass; the
whitelist drops every provider-prefixed row the operator did not list, so
a sync can reorder the picker but not re-expand it. Native Codex rows carry
no provider prefix and are never dropped by the pin, so a new built-in model
still shows up. Default: real-models.json next to this script, or
$FLEET_PIN_FILE; disable with --no-pin. A missing or unreadable file
disables the pin instead of emptying the catalog.
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
    # workbuddy leads since 2026-10-03: the only bridge whose models passed
    # both code tasks full-mark (deepseek trio, docs/code-model-selection.md),
    # so its block belongs right after the pinned leads, not fifth in line.
    "workbuddy,workbuddy-gpt,tokendance,trae,cline,stepfun,catpaw,xhx,codely,"
    "gemini,qoder,lingxi,antigravity,zcode,qwen,spacebunny")

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
    # deepseek leads workbuddy (the user's top pick); hy4 stays right behind.
    "workbuddy": ("deepseek-v4.1-flash", "deepseek-v4-pro", "deepseek-v4-flash",
                  "hy4-preview", "hy3", "glm-5.3", "glm-5.3-flash",
                  "kimi-k2.8-preview", "glm-5.2", "glm-5.1", "glm-5v-turbo",
                  "minimax-m3", "kimi-k3-1", "kimi-k2.7", "kimi-k2.6", "auto"),
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
    # Flash before the bare id: "GLM-5.3" is a substring of "GLM-5.3-Flash",
    # so the longer needle has to come first or both rows share rank 0.
    "zcode": ("GLM-5.3-Flash", "GLM-5.3"),
    "catpaw": ("glm-5.3-flashx", "glm-5.2", "glm-5.1", "deepseek-v3.2"),
    "gemini": ("gemini-3-pro-preview", "gemini-3-flash-preview",
                "gemini-2.5-pro", "gemini-2.5-flash"),
    "antigravity": ("claude-opus-4-8@default", "claude-opus-4-6@default",
                     "claude-opus-4-5@20251101", "claude-sonnet-4-5@20250929",
                     "gemini-3-flash-preview"),
    "tokendance": ("deepseek-v4.1-flash", "deepseek-v4-pro", "glm-5.3",
                     "glm-5.2", "qwen-3.7-plus"),
    "qwen": ("qwen3.8-max", "qwen-3.7-plus"),
    "spacebunny": ("space-bunny-alpha",),
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

# The model(s) the user asks to see first, pinned above every other row
# (even above their own tier, so a momentarily unreachable bridge cannot push
# the go-to off the top). The earliest listed slug wins the very first row.
#
# Configurable with FLEET_FIRST_MODEL (comma-separated <provider>/<model>) so
# the top pick changes without a code edit. hy4 was the original pick; the
# user moved the top slot to workbuddy's deepseek v4.1 flash (the CodeBuddy
# app lists Deepseek-V4.1-Flash at 0.11x; the bridge's static table lagged
# until 2026-10-03, when the model was added and routed for real).
LEAD_SLUGS = tuple(
    s.strip() for s in os.environ.get(
        # the 2026-10-03 full-fleet code bench: the only three models that
        # passed both coding tasks with full marks and zero prose. flash
        # first because it is also FLEET_DEFAULT_MODEL.
        "FLEET_FIRST_MODEL",
        "workbuddy/deepseek-v4-flash,workbuddy/deepseek-v4.1-flash,"
        "workbuddy/deepseek-v4-pro").split(",")
    if s.strip())


def is_lead(slug):
    return slug in LEAD_SLUGS


def lead_rank(slug):
    """0 for the user's top pick; each backup after it; the rest out-of-band."""
    return LEAD_SLUGS.index(slug) if slug in LEAD_SLUGS else len(LEAD_SLUGS) + 1

# How many of a provider's strongest models get floated into the head band.
# 1 left a platform's runner-up far below its own sibling: zcode/GLM-5.3 sat at
# row 69 of 117 while GLM-5.3-Flash was the provider's row 10 representative.
# 2 keeps each platform's best pair on the first screen; the band stays short
# because only listed-important models are eligible.
REP_BAND = max(1, int(os.environ.get("FLEET_REP_BAND", "2")))

def interleave_reps(models, order, good=None, families=()):
    """Float each reachable provider's strongest models to the front.

    With client-first ordering, each provider's models form a contiguous
    block. Without reps, the biggest provider (e.g. tokendance with 96
    models) would push every other provider off the first screen. Leading
    with a short band per reachable provider keeps the whole fleet visible
    at the top; the full blocks follow in order.

    The band is the first REP_BAND models of that provider per
    PER_PROVIDER_IMPORTANT. the lead model is always the very first row,
    because the user wants it pinned first.
    """
    reps, rest = [], []
    taken = set()
    pool = [m for m in models
            if good is None or provider_of(slug_of(m)) in good]
    # The lead models first — always, even before the other reps, so the user's
    # go-to models are the first rows in the picker. Every LEAD_SLUGS entry is
    # pinned in listed order: the 2026-10-03 bench produced a winning trio,
    # not a single winner, and a lone break() would have left two of the three
    # buried inside the provider band below.
    for lead_slug in LEAD_SLUGS:
        for model in pool:
            if slug_of(model) == lead_slug:
                reps.append(model)
                taken.add(lead_slug)
                break
    # A short band per reachable provider, in --order order, skipping the lead model
    # since it already leads. Picking the "most important" models per
    # provider gives the user a quick scan of the whole fleet on the first
    # screen, and keeps each platform's best pair together.
    for prov in order:
        if good is not None and prov not in good:
            continue
        band = []
        for model in pool:
            slug = slug_of(model)
            if provider_of(slug) != prov or slug in taken:
                continue
            band.append((important_rank(slug), model))
        # sort() is stable, so equal ranks keep the incoming rank order
        band.sort(key=lambda pair: pair[0])
        for _rank, model in band[:REP_BAND]:
            reps.append(model)
            taken.add(slug_of(model))
    for model in models:
        slug = model.get("slug") or model.get("id") or ""
        if slug not in taken:
            rest.append(model)
            taken.add(slug)
    return reps + rest

from catalog_common import prune_backups, read_json  # noqa: F401


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


def default_pin_path():
    """Whitelist this tool pins to unless --pin or $FLEET_PIN_FILE overrides."""
    env = os.environ.get("FLEET_PIN_FILE")
    if env:
        return env
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "real-models.json")


def load_pin(path):
    """Slugs an operator declared REAL, or None when there is no usable pin.

    An absent or unreadable file returns None and the caller then skips
    pinning: a lost whitelist must not be able to empty the catalog, which
    is the one outcome worse than a few junk rows in the picker.
    """
    if not path or not os.path.exists(path):
        print("fleet-sort: no pin file at %s; pinning disabled" % path,
              file=sys.stderr)
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        print("fleet-sort: pin file unreadable: %s; pinning disabled" % exc,
              file=sys.stderr)
        return None
    if isinstance(data, dict):
        raw = data.get("models") or data.get("slugs") or []
    elif isinstance(data, list):
        raw = data
    else:
        print("fleet-sort: pin file is %s, expected a list of slugs"
              % type(data).__name__, file=sys.stderr)
        return None
    if not isinstance(raw, list) or not all(isinstance(s, str) for s in raw):
        print("fleet-sort: pin file holds no list of slugs; pinning disabled",
              file=sys.stderr)
        return None
    return {s.strip() for s in raw if s.strip()}


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
    ap.add_argument("--pin", metavar="FILE", default="",
                    help="drop provider-prefixed rows whose slug is not "
                         "listed in FILE (default: %s)"
                         % default_pin_path())
    ap.add_argument("--no-pin", action="store_true",
                    help="keep every reachable row, ignoring the whitelist")
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
    pin = None
    if args.no_pin:
        print("fleet-sort: --no-pin given; every reachable row is kept",
              file=sys.stderr)
    else:
        pin = load_pin(args.pin or default_pin_path())

    path = args.catalog or catalog_path()
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    models = data.get("models") or []
    # The whitelist runs before anything else. A row it rejects is gone
    # before any verdict is consulted, so its provider never has to be
    # covered by the snapshot: a sync that wrote an unprobed provider
    # (kimi, tokendance, ...) must not be able to block the reorder.
    on_disk = data.get("models") or []
    catalog_slugs = {m.get("slug") or m.get("id") or "" for m in on_disk}
    pinned_out = []
    models = on_disk
    if pin is not None:
        survivors = []
        for model in on_disk:
            slug = model.get("slug") or model.get("id") or ""
            prov = provider_of(slug)
            # A provider row nobody ever verified REAL leaves again, even
            # when it would otherwise sort first.
            if prov is not None and slug not in pin:
                pinned_out.append(slug)
            else:
                survivors.append(model)
        models = survivors
    # A truncated probe run (killed mid-sweep) leaves most bridges unmeasured,
    # and those silently become "unknown" and sink below known-good rows.
    # Require the snapshot to cover the providers actually in the catalog.
    catalog_providers = {provider_of(m.get("slug") or m.get("id") or "")
                         for m in models}
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
        # The lead model is pinned first (even before the per-provider list).
        lead = lead_rank(slug)
        # Per-provider important models come next, in the listed order.
        imp = important_rank(slug)
        # Then fall back to family order and version-number (desc),
        # so pro > flash > older versions within the same family.
        fam = family_rank(slug, families)
        ver = _version_key(slug)
        return (tier, pos, lead, imp, proven, fam, ver, slug)

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
        "before": len(on_disk),
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
        "pin_file": None if pin is None else (args.pin or default_pin_path()),
        "pin_slugs": len(pin) if pin is not None else 0,
        "dropped_by_pin": len(pinned_out),
        "pin_missing_from_catalog": sorted(
            s for s in (pin or set()) if provider_of(s) is not None
            and s not in catalog_slugs),
        "first20": [m.get("slug") or m.get("id") for m in kept[:20]],
    }
    if pinned_out:
        # Say it out loud, not only in the JSON: the wrapper discards stdout,
        # and a silent cull is indistinguishable from a broken sorter.
        print("fleet-sort: pin removed %d rows across %d providers"
              % (len(pinned_out),
                 len({provider_of(s) for s in pinned_out if provider_of(s)})),
              file=sys.stderr)
    if summary["pin_missing_from_catalog"]:
        # A sync can write a degraded set: these rows are not junk, they are
        # simply absent, and only a fresh sync brings them back.
        print("fleet-sort: %d whitelisted rows are missing from the catalog: %s%s"
              % (len(summary["pin_missing_from_catalog"]),
                 ", ".join(summary["pin_missing_from_catalog"][:12]),
                 " ..." if len(summary["pin_missing_from_catalog"]) > 12 else ""),
              file=sys.stderr)

    # The picker sorts on priority, so prove reachable rows really sort first.
    slug_of = lambda m: m.get("slug") or m.get("id") or ""
    # lead rows stay out of both sets: they are pinned above their own tier
    # on purpose, so counting them would fail every sort while a bridge is down.
    good_idx = [i for i, m in enumerate(kept)
                if provider_of(slug_of(m)) in good and not is_lead(slug_of(m))]
    bad_idx = [i for i, m in enumerate(kept)
               if provider_of(slug_of(m)) in sinking and not is_lead(slug_of(m))]
    good_pri = [m["priority"] for m in kept
                if provider_of(slug_of(m)) in good
                and provider_of(slug_of(m)) is not None
                and not is_lead(slug_of(m))]
    bad_pri = [m["priority"] for m in kept
               if provider_of(slug_of(m)) in sinking and not is_lead(slug_of(m))]
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

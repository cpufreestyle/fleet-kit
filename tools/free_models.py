#!/usr/bin/env python3
"""free_models.py - annotate every fleet model with free-tier status, time windows,
and whether the call bills the reverse-proxied client's own credits.

Data source: free-windows.json (repo root) - edit that file to change annotations.
Live models:  ocx models live --json   (falls back to catalog-only when ocx is absent)
Picker state: ~/.codex/cc-switch-model-catalog.json (slug list)

credits kinds (legend.credits):
  client  走客户端积分：消耗被反代理客户端账号内的点数/tokens/次数，可扣完
  limit   不走积分，只占账号免费限额：官方不计点，仅限速/限次
  own     不走客户端积分：独立 API Key 余额、官方按量付费或原生订阅
  unknown 官网未公示或当前不可达

code capability (code-capability.json, written by code_model_bench.py snapshot):
  FULL / PARTIAL / NORUN / DEAD = the verdict of running the two real coding
  tasks through the bridge; missing or older than stale_after_days days is
  stamped stale instead of dropped, so a dated run always beats a vibe.

Usage:
  free_models.py                  human table
  free_models.py --json           machine-readable snapshot
  free_models.py --free-only      only free/free-window/quota/trial rows
  free_models.py --provider P     filter one provider
  free_models.py --credits K      only client / limit / own / unknown rows
  free_models.py --missing        picker gap report only
  free_models.py --check-sources  probe every official source URL for reachability
"""
import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "free-windows.json")
CODE_CAP_PATH = os.path.join(ROOT, "code-capability.json")
CATALOG = os.path.expanduser("~/.codex/cc-switch-model-catalog.json")
FREE_KINDS = ("free", "free-window", "quota", "trial")
BADGE = {"free": "FREE", "free-window": "LIMITED", "quota": "QUOTA",
         "trial": "TRIAL", "subscription": "SUB", "paid": "PAID",
         "unknown": "N/A", "blocked": "DOWN"}
# A coding plan bills as a subscription: the plan's own quota, not a client
# credit and not a per-key limit. Kimi Code and MiniMax are read this way.
CREDITS_KINDS = ("client", "limit", "own", "unknown", "subscription")
CREDITS_BADGE = {"client": "客户端积分", "limit": "仅限额", "own": "独立Key",
                 "subscription": "订阅套餐内含", "unknown": "N/A"}

# Free-quota windows as machine-readable bounds. "window" stays the human
# explanation of what the window is; these two are what make "is it live right
# now" a computation instead of a sentence typed on the day it was measured --
# the old "(已结束)" suffix used to sit in the table long after it stopped being
# true.
#
# Deliberately NOT parsed out of the free text: the same field also carries live
# status notes ("2026-10-02 实测账号级额度用尽"), and a date there is a
# measurement date, not a quota window. Reading one as the other would invent
# windows that never existed.
SHANGHAI = datetime.timezone(datetime.timedelta(hours=8))
WINDOW_STATES = ("active", "upcoming", "expired", "standing", "unknown")
WINDOW_BADGE = {"active": "LIVE", "upcoming": "SOON", "expired": "EXPIRED",
                "standing": "STANDING", "unknown": "?"}

# Can-write-code verdicts from kit/code-capability.json. Mirrors
# code_model_bench.STALE_AFTER_DAYS so the panel and the bench agree on when a
# measurement is too old to read as current.
CODE_BADGE = {"FULL": "全过", "PARTIAL": "部分", "NORUN": "NO_RUN",
              "DEAD": "断桥", None: "?"}
STALE_AFTER_DAYS = 7.0

def _parse_ts(value):
    """ISO 8601 -> epoch seconds, or None. A bare date is that day at midnight."""
    text = str(value or "").strip()
    if not text:
        return None
    if len(text) == 10:
        text += "T00:00:00"
    try:
        stamp = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=SHANGHAI)
    return stamp.timestamp()


def window_state(start=None, end=None, now=None):
    """Is a free-quota window live, upcoming, over, or not a window at all?

    "standing" is for a tier the vendor describes as having no end (长期免费档,
    无固定截止); it is asserted by the data, never guessed, because "no dates"
    and "no end" are different claims.
    """
    now = time.time() if now is None else now
    lo, hi = _parse_ts(start), _parse_ts(end)
    if lo is None and hi is None:
        return "unknown"
    if lo is not None and now < lo:
        return "upcoming"
    if hi is not None and now > hi:
        return "expired"
    return "active"


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
        # live "id" sometimes carries a bridge prefix (trae/X, cline-free/X)
        # while the picker slug hyphenates it (cline-free-X). The namespaced
        # spelling matches the picker; fall back to the raw id otherwise.
        model = str(item.get("namespaced") or "")
        if "/" in model:
            model = model.split("/", 1)[1]
        else:
            model = str(item.get("id") or "")
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


def load_code_cap():
    """kit/code-capability.json -> {"provider/model": row}; {} when unusable.

    A missing or half-written snapshot must never crash the panel, so every
    failure path (absent file, bad JSON, wrong shape) degrades to "no data"
    and the rows simply lose their code badge.
    """
    try:
        with open(CODE_CAP_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return {}
    models = data.get("models") if isinstance(data, dict) else None
    return models if isinstance(models, dict) else {}


def code_cap_lookup(cap, provider, model_id):
    """The one capability row for provider/model_id, or None.

    Exact provider/model wins, then same-provider suffix matches; a bare
    model id is accepted only when exactly one row fleet-wide ends with it,
    because several bridges expose deepseek-v4-pro under their own names and
    guessing would staple the wrong verdict onto a row.
    """
    if not cap:
        return None
    exact = cap.get("%s/%s" % (provider, model_id))
    if exact:
        return exact
    prefix = provider + "/"
    for key, entry in cap.items():
        if key.startswith(prefix) and key[len(prefix):].endswith(model_id):
            return entry
    if "/" in model_id:
        return None
    hits = [entry for key, entry in cap.items() if key.endswith("/" + model_id)]
    return hits[0] if len(hits) == 1 else None


def code_age_days(measured_at):
    """Days since a snapshot row was measured; None when unparseable."""
    stamp = _parse_ts(measured_at)
    return None if stamp is None else (time.time() - stamp) / 86400.0
def annotate(db, provider, model, cap=None):
    if cap is None:
        cap = load_code_cap()
    entry = code_cap_lookup(cap, provider, model) or {}
    age = code_age_days(entry.get("measured_at"))
    verdict = entry.get("verdict")
    key = "%s/%s" % (provider, model)
    override = db.get("models", {}).get(key)
    pdef = db.get("providers", {}).get(provider, {})
    free = (override or {}).get("free") or pdef.get("free") or "unknown"
    window = (override or {}).get("window") or pdef.get("window") or ""
    source = (override or {}).get("source") or pdef.get("site") or ""
    verified = (override or {}).get("verified", pdef.get("verified", False))
    credits = (override or {}).get("credits") or pdef.get("credits") or "unknown"
    credits_note = (override or {}).get("credits_note") or pdef.get("credits_note") or ""
    start = (override or {}).get("window_start") or pdef.get("window_start") or ""
    end = (override or {}).get("window_end") or pdef.get("window_end") or ""
    # standing is the one state no date can produce, so the row has to say it
    # was asserted rather than computed; otherwise a reader cannot tell "the
    # vendor says no end" from "nobody has looked yet".
    standing = bool((override or {}).get("window_standing",
                                        pdef.get("window_standing", False)))
    stale = age is not None and age > STALE_AFTER_DAYS
    state = "standing" if (standing and not start and not end) \
        else window_state(start, end)
    return {"model": key, "provider": provider, "model_id": model,
            "free": free, "badge": BADGE.get(free, free),
            "credits": credits, "credits_badge": CREDITS_BADGE.get(credits, credits),
            "credits_note": credits_note,
            "window": window, "window_start": start, "window_end": end,
            "window_standing": standing,
            "window_state": state, "state_badge": WINDOW_BADGE[state],
             "source": source, "verified": bool(verified),
             "code_verdict": verdict,
             "code_badge": CODE_BADGE.get(verdict, "?"),
            "code_seconds": entry.get("total_seconds"),
            "code_at": entry.get("measured_at"),
            "code_stale": bool(stale)}


def build():
    db = load_db()
    cap = load_code_cap()
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
        row = annotate(db, provider, model, cap)
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
    credits_counts = {}
    for row in models:
        credits_counts[row["credits"]] = credits_counts.get(row["credits"], 0) + 1
    code_counts = {}
    for row in models:
        code_counts[row["code_verdict"] or "none"] = \
            code_counts.get(row["code_verdict"] or "none", 0) + 1
    state_counts = {}
    for row in models:
        state_counts[row["window_state"]] = \
            state_counts.get(row["window_state"], 0) + 1
    return {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "db_updated": db.get("updated", "?"),
            "live_source": source_kind,
            "catalog_total": len(slugs),
            "counts": counts,
            "credits_counts": credits_counts,
            "code_counts": code_counts,
            "code_stale_after_days": STALE_AFTER_DAYS,
            "window_states": state_counts,
            "live_by_provider": live_by_provider,
            "picker_by_provider": picker_by_provider,
            "providers": db.get("providers", {}),
            "models": models,
            "gaps": db.get("gaps", []),
            "legend": db.get("legend", {})}


def db_legend_text(snap, kind):
    """One-line meaning of a credits kind, taken from free-windows.json legend."""
    legend = snap.get("legend", {}).get("credits", {})
    if isinstance(legend, dict):
        return legend.get(kind, "")
    for item in legend:
        if item.get("key") == kind:
            return item.get("text", "")
    return ""


def print_table(snap, free_only=False, provider=None, credits=None):
    rows = snap["models"]
    if free_only:
        rows = [r for r in rows if r["free"] in FREE_KINDS]
    if provider:
        rows = [r for r in rows if r["provider"] == provider]
    if credits:
        rows = [r for r in rows if r["credits"] == credits]
    print("fleet model annotations (live source: %s, db updated %s, catalog %d entries)"
          % (snap["live_source"], snap["db_updated"], snap["catalog_total"]))
    print("credits: %s" % " | ".join(
        "%s=%s" % (CREDITS_BADGE.get(k, k), db_legend_text(snap, k))
       for k in sorted(snap.get("credits_counts", {}))))
    print("-" * 142)
    for row in rows:
        name = row["picker_name"] if row["in_picker"] else ("%s   [NOT in picker]" % row["model"])
        code = row["code_badge"] + ("!" if row["code_stale"] else "")
        print("%-30s %-9s %-11s %-9s %-8s %s"
             % (name, row["badge"], row["credits_badge"],
                 row["state_badge"], code, row["window"]))
    print("-" * 142)
    print("free   counts: " + "  ".join("%s=%d" % (BADGE.get(k, k), v)
                                       for k, v in sorted(snap["counts"].items())))
    print("credit counts: " + "  ".join("%s=%d" % (CREDITS_BADGE.get(k, k), v)
                                       for k, v in sorted(snap.get("credits_counts", {}).items())))
    print("code   counts: " + "  ".join("%s=%d" % (CODE_BADGE.get(k, k), v)
                                        for k, v in sorted(snap.get("code_counts", {}).items())))
    print("window states: " + "  ".join(
        "%s=%d" % (WINDOW_BADGE.get(k, k), v)
        for k, v in sorted(snap.get("window_states", {}).items())))
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
        status = probe_url(url)
        print("  %-55s %s" % (url, "HTTP %d" % status if status else "unreachable"))


def probe_url(url, timeout=12):
    """HTTP status for a vendor page, or None. Never raises."""
    try:
        req = urllib.request.Request(url, method="GET",
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except Exception:
        return None


def refresh_sources(db, timeout=12, only=None):
    """Re-probe every provider source, stamp verified and last_checked.

    A stale "verified: true" is the same claim the hand-typed window used to be:
    it says the page was read, with no date attached. This turns it into a
    measurement. verified stays true only while at least one official source
    still answers; a provider whose every source has gone dark drops to false
    rather than standing on the strength of an old look. An unreachable page
    proves nothing about the pricing either way, so one dead link beside a live
    one does not invalidate a record that was still read today.
    """
    checked = {}
    for name, pdef in db.get("providers", {}).items():
        if only and name not in only:
            continue
        statuses = dict((url, probe_url(url, timeout))
                        for url in (pdef.get("sources") or []))
        if statuses:
            pdef["verified"] = any(status == 200 for status in statuses.values())
        pdef["last_checked"] = time.strftime("%Y-%m-%d %H:%M:%S")
        checked[name] = {"reachable": pdef["verified"], "sources": statuses}
    return checked


def save_db(db):
    """Write the database back beside a timestamped .bak."""
    bak = "%s.bak-%s" % (DB_PATH, time.strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(DB_PATH, bak)
    with open(DB_PATH, "w", encoding="utf-8") as fh:
        json.dump(db, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return bak


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="annotate fleet models with free status and time windows")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--free-only", action="store_true", help="only free rows")
    parser.add_argument("--provider", help="filter one provider")
    parser.add_argument("--credits", choices=CREDITS_KINDS,
                        help="only rows whose call bills this kind of quota")
    parser.add_argument("--missing", action="store_true", help="picker gap report")
    parser.add_argument("--check-sources", action="store_true", help="probe source URLs")
    parser.add_argument("--refresh", action="store_true",
                        help="re-probe every source page and stamp "
                             "verified/last_checked back into the database")
    args = parser.parse_args(argv)
    if args.refresh:
        db = load_db()
        checked = refresh_sources(
            db, only={args.provider} if args.provider else None)
        bak = save_db(db)
        print("refreshed %d providers; backup: %s" % (len(checked), bak))
        for name in sorted(checked):
            row = checked[name]
            print("  %-14s %s" % (name,
                                   "reachable" if row["reachable"] else "unreachable"))
            for url, status in sorted(row["sources"].items()):
                print("      %-52s %s"
                      % (url, status if status else "unreachable"))
        return 0
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
    print_table(snap, free_only=args.free_only, provider=args.provider,
                credits=args.credits)
    return 0


if __name__ == "__main__":
    sys.exit(main())

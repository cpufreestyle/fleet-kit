#!/usr/bin/env python3
"""Shrink the Codex catalog to the providers that still have a pulse.

Why this exists
---------------
The picker offered 242 models, but a count is not capacity: 98 of them belong
to tokendance, whose key answers 401 today, and another ~80 sit behind bridges
that have not served a successful request in weeks. catalog_filter.py hides
rows a bridge never proved REAL; it does not ask whether anything actually
flows. This tool asks the opposite question and is deliberately the *conservative*
one: it hides a provider only when all three are true

  * fleet_probe calls it unreachable,
  * it has zero successful requests in the window (default 30 days),
  * it carries no free-tier model worth keeping.

Measured 2026-10-07: that is 4 providers and 17 rows. Everything with a pulse
stays -- tokendance included, because its 401 is recent: 31,817 successful
requests in the last 30 days. A provider that died this month is a key to
renew, not a row to delete, so it is reported under "recently_dead" instead.

Usage:
  catalog_shrink.py                 report only (changes nothing)
  catalog_shrink.py --apply         rewrite the catalog, with a backup
  catalog_shrink.py --days 7        judge on a 7-day window
  catalog_shrink.py --keep qwen,... never hide these providers
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sqlite3
import sys
import time
import urllib.request

CODEX_HOME = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
DEFAULT_REACH = os.path.expanduser("~/.codex/fleet-reach.json")
DEFAULT_DB = os.path.expanduser("~/.cc-switch/cc-switch.db")
DEFAULT_FREE = os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), os.pardir, "free-windows.json"))
FREE_KINDS = ("free", "free-window", "quota", "trial")

# A model name that reaches the proxy without its provider prefix (the way
# CC Switch logs record it) still names its bridge if you know the prefixes.
PREFIX_HINTS = (
    ("xhx-sn-", "xhx"), ("xhx-", "xhx"), ("trae-", "trae"),
    ("lingxi-", "lingxi"), ("step-", "stepfun"), ("stealth-", "spacebunny"),
    ("claude-", "antigravity"), ("gemini-", "gemini"), ("qwen3", "qoder"),
    ("codely-", "codely"), ("minimax", "minimax"), ("kimi", "kimi"),
    ("glm-5", "zcode"), ("seed-main", "doubao"), ("deepseek-v4", "workbuddy"),
    ("hy3", "workbuddy"), ("hy4", "workbuddy"), ("gpt-", "workbuddy-gpt"),
)


def catalog_from_config(codex_home):
    path = os.path.join(codex_home, "config.toml")
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("model_catalog_json"):
                    return os.path.expanduser(
                        line.split("=", 1)[1].strip().strip('"'))
    except OSError:
        pass
    return os.path.join(codex_home, "opencodex-catalog.json")


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default if default is not None else {}


def usage_by_provider(db_path, days, catalog_slugs):
    """Successful requests per provider in the window, from CC Switch's log."""
    bare2pref = {}
    for slug in catalog_slugs:
        if "/" in slug:
            pref, bare = slug.split("/", 1)
            bare2pref.setdefault(bare, pref)
    total, ok = collections.Counter(), collections.Counter()
    try:
        conn = sqlite3.connect(db_path, timeout=5)
    except sqlite3.Error:
        return total, ok
    try:
        since = int(time.time()) - days * 86400
        rows = conn.execute(
            "select model, status_code from proxy_request_logs"
            " where created_at >= ?", (since,)).fetchall()
    except sqlite3.Error:
        return total, ok
    finally:
        conn.close()
    for model, code in rows:
        name = str(model or "")
        pref = None
        if "/" in name:
            pref = name.split("/", 1)[0]
        elif name in bare2pref:
            pref = bare2pref[name]
        else:
            low = name.lower()
            for hint, p in PREFIX_HINTS:
                if low.startswith(hint):
                    pref = p
                    break
        if not pref:
            continue
        total[pref] += 1
        try:
            if int(code or 0) == 200:
                ok[pref] += 1
        except (TypeError, ValueError):
            pass
    return total, ok


def free_tier_providers(path):
    data = load_json(path, {})
    models = data.get("models") if isinstance(data, dict) else data
    out = collections.Counter()
    if isinstance(models, dict):
        for slug, row in models.items():
            kind = (row or {}).get("kind") or (row or {}).get("free_kind")
            if kind in FREE_KINDS:
                out[str(slug).split("/")[0]] += 1
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--codex-home", default=CODEX_HOME)
    ap.add_argument("--catalog")
    ap.add_argument("--reach", default=DEFAULT_REACH)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--free-windows", default=DEFAULT_FREE)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--keep", default="", help="never hide these providers")
    ap.add_argument("--apply", action="store_true",
                    help="rewrite the catalog (default is report only)")
    args = ap.parse_args(argv)

    catalog_path = args.catalog or catalog_from_config(args.codex_home)
    data = load_json(catalog_path, {})
    models = data.get("models")
    if not isinstance(models, list):
        print("catalog unreadable or has no models list: %s" % catalog_path)
        return 5

    slugs = [str(m.get("slug") or m.get("id") or "") for m in models
             if isinstance(m, dict)]
    providers = collections.Counter(s.split("/")[0] if "/" in s else "(bare)"
                                    for s in slugs)
    reach = load_json(args.reach, {})
    reachable = set(reach.get("reachable") or [])
    unreachable = set(reach.get("unreachable") or [])
    _, ok = usage_by_provider(args.db, args.days, slugs)
    free = free_tier_providers(args.free_windows)
    keep = {p.strip() for p in args.keep.split(",") if p.strip()}

    hidden, kept, recently_dead = [], [], []
    for name in sorted(providers):
        row = {"provider": name, "models": providers[name],
               "reachable": name in reachable,
               "unreachable": name in unreachable,
               "ok_%sd" % args.days: ok.get(name, 0),
               "free_models": free.get(name, 0)}
        if name in keep:
            kept.append(row)
            continue
        # Conservative on purpose: a provider is only dropped when it is
        # measured unreachable, produced nothing in the window, and has no
        # free-tier model worth keeping.
        if row["unreachable"] and not row["ok_%sd" % args.days] \
                and not row["free_models"]:
            hidden.append(row)
        else:
            kept.append(row)
            if row["unreachable"] and row["ok_%sd" % args.days]:
                recently_dead.append(row)

    hide = {r["provider"] for r in hidden}
    keepers = [m for m in models
               if (str(m.get("slug") or m.get("id") or "").split("/")[0]
                   if "/" in str(m.get("slug") or m.get("id") or "")
                   else "(bare)") not in hide]

    summary = {
        "catalog": catalog_path, "days": args.days,
        "models_before": len(models), "models_after": len(keepers),
        "removed": len(models) - len(keepers),
        "hidden_providers": hidden, "kept_providers": kept,
        "recently_dead": recently_dead, "applied": False,
    }

    if args.apply and hide:
        backup = "%s.bak-before-shrink-%s" % (
            catalog_path, time.strftime("%Y%m%d-%H%M%S"))
        try:
            with open(backup, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
            data["models"] = keepers
            with open(catalog_path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
            summary["applied"] = True
            summary["backup"] = backup
        except OSError as exc:
            summary["write_error"] = str(exc)

    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())

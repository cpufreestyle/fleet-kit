#!/usr/bin/env python3
"""FleetKit Codex default-model guard.

Codex's model picker is driven by ~/.codex/opencodex-catalog.json. That file is
synced by `ocx sync` and may contain models that route to a local FleetKit bridge
(e.g. qoder/*, cline/*, workbuddy/*). When the bridge fleet is NOT running - the
normal state on a host that has not deployed FleetKit - picking one of those
models yields the exact error the user keeps hitting:

    502 Bad Gateway: Provider unreachable: Unable to connect.
    url: http://127.0.0.1:10100/v1/responses

This script does two things:

  1. Shows the current default model (from ~/.codex/config.toml) and whether it
     is safe to pick right now.
  2. With --guard, prunes the catalog down to models that are reachable WITHOUT
     a running bridge fleet, so the picker can no longer offer a guaranteed-502
     model. A timestamped backup is written first; `ocx sync` restores the full
     list later if you deploy the fleet.

Reachability model (this host, no fleet deployed):
  - stepfun/* and bare step-* are official StepFun endpoints, no bridge -> SAFE
  - gpt-* are OpenAI models served through Codex's own account pool, no bridge
    -> SAFE (may 401 if not signed in, but never 502)
  - everything else (workbuddy, qoder, cline, codely, trae, lingxi, xhx, gemini,
    catpaw, antigravity, qwen, zcode, ...) routes to a local bridge -> DEAD now

Usage:
  fleet_default_model.py [--catalog PATH] [--home DIR]
                         [--safe-prefix stepfun gpt ...]
                         [--list] [--check] [--guard] [--dry-run]
                         [--apply-default] [--json]

Exit codes:
  0 safe (or guard wrote)    1 unsafe default / nothing pruned    2 usage error
"""
import argparse
import json
import os
import re
import shutil
import sys

DEFAULT_CATALOG = os.path.join(
    os.path.expanduser("~"), ".codex", "opencodex-catalog.json")
DEFAULT_CONFIG = os.path.join(os.path.expanduser("~"), ".codex", "config.toml")


def resolve_catalog(arg):
    if arg:
        return os.path.abspath(os.path.expanduser(arg))
    env = os.environ.get("OPENCODEX_CATALOG")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return DEFAULT_CATALOG


def load_catalog(path):
    if not os.path.isfile(path):
        return None, "catalog not found: %s" % path
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, list):
        return data, None
    if isinstance(data, dict) and "models" in data:
        return data["models"], None
    return None, "unexpected catalog shape (need a list or {\"models\": [...]})"


def save_catalog(path, models, original):
    if isinstance(original, list):
        payload = models
    else:
        payload = dict(original)
        payload["models"] = models
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def slug_of(row):
    if isinstance(row, dict):
        return str(row.get("slug") or row.get("id") or row.get("name") or "")
    return str(row)


def is_safe(slug, safe):
    low = slug.lower()
    return any(low.startswith(p.lower()) for p in safe)


def recommended_default(models, safe):
    slugs = [slug_of(m) for m in models]
    order = ["step-5-preview"]
    for s in order:
        if s in slugs:
            return s
    step_direct = [s for s in slugs if s.lower().startswith("step-")
                   and "/" not in s]
    if step_direct:
        return step_direct[0]
    step_ns = [s for s in slugs if s.lower().startswith("stepfun/")]
    if step_ns:
        return step_ns[0]
    gpts = [s for s in slugs if s.lower().startswith("gpt")]
    if gpts:
        return gpts[0]
    safes = [s for s in slugs if is_safe(s, safe)]
    return safes[0] if safes else ""


def read_default_model(config_path):
    if not os.path.isfile(config_path):
        return ""
    with open(config_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line.startswith("#"):
                continue
            if re.match(r"^model\s*=", line):
                _, _, val = line.partition("=")
                return val.strip().strip('"').strip("'")
    return ""


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    default_kit = os.path.abspath(os.path.join(here, "..", "..", "..", ".."))
    ap = argparse.ArgumentParser(description="FleetKit Codex default-model guard")
    ap.add_argument("--catalog", default=None)
    ap.add_argument("--home", default=os.path.join(os.path.expanduser("~"),
                                                   "FleetKit", "runtime"))
    ap.add_argument("--kit", default=default_kit)
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--safe-prefix", nargs="*", default=["stepfun", "step-", "gpt"])
    ap.add_argument("--list", action="store_true", help="list every model + verdict")
    ap.add_argument("--check", action="store_true", help="show default + safe status")
    ap.add_argument("--guard", action="store_true",
                    help="prune catalog to safe models (writes a .bak backup)")
    ap.add_argument("--dry-run", action="store_true",
                    help="with --guard, preview only, write nothing")
    ap.add_argument("--apply-default", action="store_true",
                    help="write the recommended safe default into config.toml")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    safe = args.safe_prefix
    path = resolve_catalog(args.catalog)
    models, err = load_catalog(path)
    if err:
        print(err, file=sys.stderr)
        return 2

    verdicts = []
    for m in models:
        s = slug_of(m)
        verdicts.append({"slug": s, "safe": is_safe(s, safe)})

    if args.list or not (args.check or args.guard or args.apply_default):
        if args.json:
            print(json.dumps({"catalog": path, "safe_prefixes": safe,
                              "models": verdicts}, indent=2, ensure_ascii=False))
        else:
            print("FleetKit default-model guard  catalog=%s" % path)
            for v in verdicts:
                print("  [%-5s] %s" % ("SAFE" if v["safe"] else "DEAD", v["slug"]))
            print()

    default_model = read_default_model(args.config)
    rec = recommended_default(models, safe)
    current_safe = bool(default_model) and is_safe(default_model, safe)

    if args.check or not (args.guard or args.apply_default):
        if args.json:
            print(json.dumps({"default_model": default_model,
                              "default_safe": current_safe,
                              "recommended_default": rec},
                             indent=2, ensure_ascii=False))
        else:
            print("current default : %s" % (default_model or "(unset)"))
            print("default safe    : %s" % ("yes" if current_safe else "NO - will 502/401"))
            print("recommended     : %s" % (rec or "(none safe in catalog)"))
            if not current_safe:
                print("fix: pick %s in Codex, or run with --guard then --apply-default"
                      % (rec or "<a safe model>"))

    if args.apply_default and rec:
        cfg = os.path.abspath(os.path.expanduser(args.config))
        text = ""
        if os.path.isfile(cfg):
            with open(cfg, encoding="utf-8") as fh:
                text = fh.read()
        lines = text.splitlines()
        kept = [ln for ln in lines if not (ln.strip().startswith("model")
                and "=" in ln and not ln.strip().startswith("#"))]
        kept.append('model = "%s"' % rec)
        with open(cfg, "w", encoding="utf-8") as fh:
            fh.write("\n".join(kept) + "\n")
        if args.json:
            print(json.dumps({"applied_default": rec}, ensure_ascii=False))
        else:
            print("wrote default model = %s into %s" % (rec, cfg))

    if args.guard:
        keep = [m for m in models if is_safe(slug_of(m), safe)]
        drop = [m for m in models if not is_safe(slug_of(m), safe)]
        if args.dry_run:
            if args.json:
                print(json.dumps({"dry_run": True, "keep": len(keep),
                                  "drop": [slug_of(m) for m in drop]},
                                 ensure_ascii=False))
            else:
                print("[dry-run] would keep %d, remove %d:" % (len(keep), len(drop)))
                for m in drop:
                    print("  - %s" % slug_of(m))
            return 0 if keep else 1
        if not drop:
            print("nothing to prune (all %d models already safe)" % len(models))
            return 0
        bak = path + ".bak"
        shutil.copy2(path, bak)
        save_catalog(path, keep, _orig(models, path))
        if args.json:
            print(json.dumps({"guarded": True, "backup": bak,
                              "kept": len(keep), "removed": len(drop),
                              "removed_slugs": [slug_of(m) for m in drop]},
                             ensure_ascii=False))
        else:
            print("backup        : %s" % bak)
            print("kept %d, removed %d dead bridge models:" % (len(keep), len(drop)))
            for m in drop:
                print("  - %s" % slug_of(m))
            print("restart Codex/ChatGPT to refresh the picker; 'ocx sync' restores them")
        return 0

    return 0 if current_safe else 1


def _orig(models, path):
    """Return the original parsed catalog so save_catalog can preserve its shape."""
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


if __name__ == "__main__":
    sys.exit(main())

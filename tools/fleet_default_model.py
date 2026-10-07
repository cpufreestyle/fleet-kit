#!/usr/bin/env python3
"""fleet_default_model.py - keep one known-good model as the Codex default.

The Codex picker keeps ~150 rows, most of them reverse-proxied bridges that can
die at any time (expired session, VPN off, upstream shutdown). When the pinned
default sits on one of them, every new session starts with a model that fails,
and the failure looks like the proxy being broken rather than one bridge.

The rule this script enforces:

  the default model is always `step-5-preview` (StepFun official Plan API,
  direct, no local bridge); when the pinned model - or any provider in the
  catalog - stops answering, fall back to it and hide the broken rows.

It is deliberately panel-free (unlike catalog_filter.py): the fallback decision
must work when the status panel itself is down, so reachability is measured by
one real request per provider through the proxy at 127.0.0.1:10100.

Usage:
  fleet_default_model.py                     pin the fallback as default (idempotent)
  fleet_default_model.py --status            show the current pin and probe it
  fleet_default_model.py --guard             probe every catalog provider, re-pin
                                             when the pin is broken, hide broken rows
  fleet_default_model.py --guard --dry-run   report only, change nothing
  fleet_default_model.py --fallback SLUG     override the fallback slug
  fleet_default_model.py --json              machine readable

Exit codes:
  0 ok   1 usage/config problem   2 fallback itself unreachable
"""
import argparse
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request

DEFAULT_FALLBACK = "step-5-preview"
PROXY_URL = "http://127.0.0.1:10100/v1/responses"


def codex_home():
    return os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")


def read_pinned_model(config_path):
    if not os.path.isfile(config_path):
        return None
    with open(config_path, encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if stripped.startswith("model =") or stripped.startswith("model="):
                return stripped.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def write_pinned_model(config_path, slug):
    with open(config_path, encoding="utf-8") as fh:
        lines = fh.readlines()
    out, pinned = [], False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("model =") or stripped.startswith("model="):
            out.append('model = "%s"\n' % slug)
            pinned = True
        else:
            out.append(line)
    if not pinned:
        for i, line in enumerate(out):
            if line.strip().startswith("model_provider"):
                out.insert(i + 1, 'model = "%s"\n' % slug)
                break
        else:
            out.append('model = "%s"\n' % slug)
    backup = config_path + ".bak"
    shutil.copyfile(config_path, backup)
    with open(config_path, "w", encoding="utf-8") as fh:
        fh.writelines(out)


def catalog_path(config_path, codex_dir):
    try:
        with open(config_path, encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if stripped.startswith("model_catalog_json"):
                    name = stripped.split("=", 1)[1].strip().strip('"').strip("'")
                    return name if os.path.isabs(name) else os.path.join(codex_dir, name)
    except OSError:
        pass
    return os.path.join(codex_dir, "opencodex-catalog.json")


def load_rows(path):
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    rows = data.get("models") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        slug = row.get("slug") or row.get("id") or row.get("model") or ""
        if slug:
            out.append((slug, row))
    return out


JUNK_TOKENS = ("tts", "speech", "embedding", "rerank", "ocr", "-asr",
               "-i2v", "-r2v", "-t2v", "web-search", "web-reader", "seedream")


def is_junk(slug):
    low = slug.lower()
    return any(token in low for token in JUNK_TOKENS)


def probe(slug, timeout=45):
    """One real request through the proxy. Returns (ok, detail)."""
    payload = json.dumps({"model": slug, "input": "ping",
                          "max_output_tokens": 16}).encode("utf-8")
    req = urllib.request.Request(PROXY_URL, data=payload, method="POST",
                                 headers={"Content-Type": "application/json"})
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, "HTTP %d (%.1fs)" % (resp.status, time.time() - started)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:120]
        return False, "HTTP %d %s" % (exc.code, body.strip())
    except Exception as exc:  # noqa: BLE001 - report, do not raise
        return False, str(exc)[:120]


def hide_providers(path, providers, dry_run):
    """Drop catalog rows whose slash prefix is in providers; returns rows removed."""
    if not providers or dry_run:
        return 0
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    key = "models" if isinstance(data, dict) else None
    rows = data[key] if key else data
    kept = [r for r in rows
            if not str((r.get("slug") or r.get("id") or r.get("model") or ""))
            .split("/")[0] in providers]
    removed = len(rows) - len(kept)
    if removed:
        shutil.copyfile(path, path + time.strftime(".bak-%Y%m%d-%H%M%S"))
        target = key if key else None
        if target:
            data[target] = kept
            payload = data
        else:
            payload = kept
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
    return removed


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--status", action="store_true", help="show the pin and probe it")
    ap.add_argument("--guard", action="store_true",
                    help="probe every provider, re-pin and hide broken rows")
    ap.add_argument("--dry-run", action="store_true", help="report only")
    ap.add_argument("--fallback", default=os.environ.get("FLEET_DEFAULT_MODEL",
                                                         DEFAULT_FALLBACK))
    ap.add_argument("--no-hide", action="store_true", help="never touch the catalog")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--timeout", type=float, default=45)
    args = ap.parse_args()

    home = codex_home()
    config_path = os.path.join(home, "config.toml")
    if not os.path.isfile(config_path):
        print("fleet-default: no config.toml at %s" % config_path, file=sys.stderr)
        return 1
    cat_path = catalog_path(config_path, home)
    pinned = read_pinned_model(config_path)

    result = {"codex_home": home, "catalog": cat_path, "pinned": pinned,
              "fallback": args.fallback, "dry_run": args.dry_run}

    fallback_ok, fallback_detail = probe(args.fallback, args.timeout)
    result["fallback_probe"] = {"ok": fallback_ok, "detail": fallback_detail}
    if not fallback_ok:
        result["error"] = "fallback itself unreachable"
        print(json.dumps(result, indent=2, ensure_ascii=False) if args.json else
              "fleet-default: fallback %s unreachable: %s\n"
              "fix the fallback provider (StepFun key / network) before guarding"
              % (args.fallback, fallback_detail), file=sys.stderr)
        return 2

    if args.status or not args.guard:
        ok, detail = (True, "is the fallback") if pinned == args.fallback else probe(
            pinned or args.fallback, args.timeout)
        result["pinned_probe"] = {"ok": ok, "detail": detail}
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print("fleet-default")
            print("  pinned   : %s" % pinned)
            print("  fallback : %s (%s)" % (args.fallback, fallback_detail))
            print("  pin probe: %s %s" % ("ok" if ok else "FAIL", detail))
        if not args.guard:
            if pinned != args.fallback:
                if args.dry_run:
                    print("  [dry-run] would pin %s" % args.fallback)
                else:
                    write_pinned_model(config_path, args.fallback)
                    print("  pinned fallback: %s (backup %s.bak)"
                          % (args.fallback, config_path))
            return 0
        return 0 if ok else 2

    # --guard
    rows = load_rows(cat_path)
    # One model per provider is not enough: a provider can serve a family where
    # some ids are gated ("this model is not enabled for the Responses API") while
    # others work, and a junk row (tts/asr/embedding) is never a chat model. So
    # collect a few candidates per provider and call it broken only if all fail.
    providers = {}
    for slug, _row in rows:
        if "/" in slug:
            providers.setdefault(slug.split("/")[0], []).append(slug)
    fallback_model = args.fallback.split("/")[-1]
    broken, checked = [], []
    for name in sorted(providers):
        slugs = [s for s in providers[name] if not is_junk(s)]
        slugs.sort(key=lambda s: (s.split("/")[-1] != fallback_model,))
        samples = slugs[:3] or providers[name][:1]
        ok, detail, used = False, "", ""
        for sample in samples:
            ok, detail = probe(sample, args.timeout)
            used = sample
            if ok:
                break
        checked.append({"provider": name, "sample": used, "ok": ok, "detail": detail,
                        "tried": len(samples)})
        if not ok:
            broken.append(name)
    result["checked"] = checked
    result["broken"] = broken

    pinned_ok, pinned_detail = probe(pinned or args.fallback, args.timeout)
    result["pinned_probe"] = {"ok": pinned_ok, "detail": pinned_detail}

    changed = False
    if pinned != args.fallback and not pinned_ok:
        if not args.dry_run:
            write_pinned_model(config_path, args.fallback)
        changed = True
        result["repinned"] = args.fallback
    removed = 0
    if broken and not args.no_hide:
        removed = hide_providers(cat_path, set(broken), args.dry_run)
    result["hidden_rows"] = removed

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print("fleet-default guard%s" % (" (dry-run)" if args.dry_run else ""))
        print("  fallback : %s ok (%s)" % (args.fallback, fallback_detail))
        for c in checked:
            print("  [%-4s] %-14s %s" % ("ok" if c["ok"] else "BAD",
                                         c["provider"], c["detail"][:70]))
        print("  pinned   : %s (%s)" % (pinned, "ok" if pinned_ok else pinned_detail))
        if changed:
            print("  re-pinned to %s" % args.fallback)
        if broken:
            print("  broken providers: %s" % ", ".join(broken))
            print("  catalog rows hidden: %d" % removed)
        if changed or removed:
            print("  restart Codex/ChatGPT so the picker reloads the catalog")
    return 0


if __name__ == "__main__":
    sys.exit(main())

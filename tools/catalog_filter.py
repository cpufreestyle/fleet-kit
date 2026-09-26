#!/usr/bin/env python3
"""catalog_filter.py - hide reverse-proxied models whose bridge is not really working.

The Codex model picker renders every entry in the catalog that model_catalog_json
names (~/.codex/cc-switch-model-catalog.json). ocx sync appends one entry per
advertised bridge model, so a dead bridge keeps showing selectable-but-broken options
in the picker: picking codely/codely-air fails at request time instead of pick time.

This tool drops catalog entries whose slash prefix names a fleet bridge that the status
panel could not verify as REAL (see verify_real_calls.py verdicts). A provider with no
bridge - tokendance, stepfun, the native openai rows - is never touched, because there
is nothing to verify it against.

Removing is only half the job. A bridge that comes back (renewed token, VPN on,
upstream fixed) would otherwise stay missing from the picker forever, because nothing
re-adds it: the ocx-catalog-guard timer only re-syncs when the bridge model count drops
below 60, and 179 surviving rows never trip that. So a provider that verifies REAL yet
has no rows left triggers one ocx sync before filtering, which restores the fleet.

Usage:
  catalog_filter.py            filter the catalog, back it up, report counts
  catalog_filter.py --dry-run  report what would be dropped, change nothing

Options:
  --status-url URL   fleet status panel (default http://127.0.0.1:8796/api/status)
  --codex-home DIR   Codex home (default ~/.codex, honours $CODEX_HOME)
  --catalog PATH     catalog file, overrides model_catalog_json
  --keep P[,P...]    providers to keep even when unavailable
  --only P[,P...]    only consider these providers for removal
  --no-backup        skip the timestamped backup
  --no-restore       do not run ocx sync to bring back a verified-REAL provider
  --timeout SEC      status panel and subprocess timeout (default 20)
  --dry-run          report only, change nothing

Exit codes: 0 ok (or nothing to do), 2 bad usage, 3 status panel unreachable,
4 nothing verified as REAL (refuses to filter everything), 5 catalog problem.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

BACKUP_TEMPLATE = ".bak-%Y%m%d-%H%M%S"
STATE_FILE = ".catalog-filter-restore-state.json"
STATE_COOLDOWN = 3600


def fail(message, code):
    print("catalog-filter: %s" % message, file=sys.stderr)
    return code


def catalog_path(codex_home, override):
    """The catalog file model_catalog_json names, same lookup as ocx-catalog-guard.sh."""
    if override:
        return override
    name = None
    config = os.path.join(codex_home, "config.toml")
    try:
        with open(config, encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if stripped.startswith("model_catalog_json"):
                    name = stripped.split("=", 1)[1].strip().strip(chr(34))
                    break
    except OSError:
        pass
    if not name:
        return None
    return name if os.path.isabs(name) else os.path.join(codex_home, name)


def load_catalog(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def fetch_status(url, timeout):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))


def ocx(args, timeout):
    """Run an ocx subcommand; returns (ok, last-output-line)."""
    try:
        out = subprocess.run(["ocx"] + args, capture_output=True, timeout=timeout)
    except Exception as exc:
        return False, "%s failed: %s" % (" ".join(args), exc)
    tail = (out.stdout or b"").decode("utf-8", "ignore").strip().splitlines()
    message = tail[-1] if tail else "rc=%d" % out.returncode
    return out.returncode == 0, message


def provider_of(model):
    """Slash prefix of a catalog slug, or None for native rows without one."""
    if not isinstance(model, dict):
        return None
    slug = model.get("slug") or model.get("id") or model.get("model") or ""
    if "/" not in slug:
        return None
    return slug.split("/", 1)[0]


def state_path(codex_home):
    return os.path.join(codex_home, STATE_FILE)


def read_state(codex_home):
    """{provider: epoch-of-last-restore-attempt}, empty when never written."""
    try:
        with open(state_path(codex_home), encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def write_state(codex_home, state):
    directory = os.path.dirname(state_path(codex_home)) or "."
    handle, tmp = tempfile.mkstemp(prefix=".catalog-filter-state-", dir=directory)
    with os.fdopen(handle, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
    os.replace(tmp, state_path(codex_home))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="hide catalog models whose bridge is not verified REAL",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--status-url", default=os.environ.get(
        "FLEET_STATUS_URL", "http://127.0.0.1:8796/api/status"))
    parser.add_argument("--codex-home",
                        default=os.environ.get("CODEX_HOME")
                        or os.path.expanduser("~/.codex"))
    parser.add_argument("--catalog")
    parser.add_argument("--keep", default="")
    parser.add_argument("--only", default="")
    parser.add_argument("--no-backup", action="store_true")
    parser.add_argument("--no-restore", action="store_true")
    parser.add_argument("--restore-cooldown", type=int, default=STATE_COOLDOWN)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    keep = {p.strip() for p in args.keep.split(",") if p.strip()}
    only = {p.strip() for p in args.only.split(",") if p.strip()}

    path = catalog_path(args.codex_home, args.catalog)
    if not path:
        return fail("no model_catalog_json in %s/config.toml" % args.codex_home, 5)
    if not os.path.exists(path):
        return fail("catalog not found: %s" % path, 5)

    try:
        data = load_catalog(path)
    except Exception as exc:
        return fail("catalog unreadable (%s): %s" % (path, exc), 5)
    models = data.get("models")
    if not isinstance(models, list):
        return fail("catalog has no models list: %s" % path, 5)

    try:
        status = fetch_status(args.status_url, args.timeout)
    except Exception as exc:
        return fail("status panel unreachable (%s): %s" % (args.status_url, exc), 3)

    bridges = {b.get("name") for b in (status.get("bridges") or [])
               if isinstance(b, dict) and b.get("name")}
    verify = status.get("verify") or {}
    real = {r for r in (verify.get("real") or []) if isinstance(r, str)}

    summary = {
        "catalog": path,
        "status_url": args.status_url,
        "verified_at": verify.get("generated_at"),
        "bridges": sorted(bridges),
        "real": sorted(real),
        "keep": sorted(keep),
        "models_before": len(models),
    }

    if not real:
        summary["error"] = "no bridge verified REAL; refusing to filter"
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return fail("no bridge verified REAL, would drop every bridged model", 4)

    present = {p for p in (provider_of(m) for m in models) if p}
    # A REAL provider with no rows left is how a recovered bridge stays invisible.
    suspects = sorted((real & bridges) - present - keep)
    if suspects and not args.dry_run and not args.no_restore:
        # ocx sync rewrites whatever catalog config.toml names, so restoring while
        # filtering some other file would corrupt the wrong one.
        # ocx sync rewrites whatever config.toml names, never --catalog, so restoring
        # while filtering some other file would rewrite a catalog we are not editing.
        default_path = catalog_path(args.codex_home, None)
        owns_catalog = (args.catalog is None
                        or (default_path is not None
                            and os.path.realpath(args.catalog)
                            == os.path.realpath(default_path)))
        state = read_state(args.codex_home) if owns_catalog else {}
        now = int(time.time())
        due = [p for p in suspects
               if now - int(state.get(p, 0)) >= args.restore_cooldown]
        if not owns_catalog:
            summary["restore_skipped"] = "--catalog overrides the ocx-owned file"
        elif not due:
            summary["restore_skipped"] = "cooldown %ss not elapsed for %s" % (
                args.restore_cooldown, ",".join(suspects))
        else:
            ok, message = ocx(["sync"], args.timeout)
            summary["restore_candidates"] = due
            summary["ocx_sync"] = message
            state.update({p: now for p in due})
            write_state(args.codex_home, state)
            if ok:
                try:
                    data = load_catalog(path)
                    models = data.get("models") or []
                    summary["models_before"] = len(models)
                    summary["restored"] = sorted(
                        ((real & bridges) & {provider_of(m) for m in models}) - present)
                except Exception as exc:
                    return fail("catalog unreadable after ocx sync: %s" % exc, 5)

    # Only bridges can be judged: a provider with no bridge has no verdict to act on.
    candidates = bridges - keep
    if only:
        candidates &= only
    unavailable = sorted(candidates - real)
    summary["unavailable"] = unavailable

    keepers, dropped = [], {}
    for model in models:
        provider = provider_of(model)
        if provider in unavailable:
            slug = model.get("slug") or model.get("id") or model.get("model") or "?"
            dropped.setdefault(provider, []).append(slug)
        else:
            keepers.append(model)

    summary["removed"] = len(models) - len(keepers)
    summary["removed_by_provider"] = {k: len(v) for k, v in sorted(dropped.items())}
    summary["removed_slugs"] = {k: v for k, v in sorted(dropped.items())}
    summary["models_after"] = len(keepers)
    data["models"] = keepers


    if args.dry_run or not dropped:
        if args.dry_run:
            summary["dry_run"] = True
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0

    # Re-serialising must round-trip byte for byte, or the write would reformat a file
    # that both Codex and CC Switch read.
    with open(path, encoding="utf-8") as fh:
        original = fh.read()
    try:
        proof = json.dumps(load_catalog(path), indent=2, ensure_ascii=False) + "\n"
    except Exception as exc:
        return fail("cannot prove round-trip (%s)" % exc, 5)
    if proof != original:
        return fail("round-trip mismatch, refusing to rewrite %s" % path, 5)

    if not args.no_backup:
        backup = path + time.strftime(BACKUP_TEMPLATE)
        if os.path.exists(backup):
            backup = "%s-%d" % (backup, os.getpid())
        with open(path, "rb") as src, open(backup, "wb") as dst:
            dst.write(src.read())
        summary["backup"] = backup

    payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    directory = os.path.dirname(path) or "."
    handle, tmp = tempfile.mkstemp(prefix=".catalog-filter-", dir=directory)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    # The picker is worthless if the file ends up broken, so prove that after writing.
    try:
        after = load_catalog(path)
    except Exception as exc:
        print("catalog-filter: WRITE LEFT AN UNREADABLE FILE: %s" % exc,
              file=sys.stderr)
        return 5
    if len(after.get("models") or []) != len(keepers):
        print("catalog-filter: WRITE LEFT THE WRONG MODEL COUNT", file=sys.stderr)
        return 5
    summary["verified_after_write"] = True
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

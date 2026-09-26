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

It also hides junk rows - TTS/voice, embeddings, rerank, OCR, speech-to-text,
image/video generation, web-search tools, subagents and a placeholder row - that only
add noise to the picker. Junk hiding is decided from the slug alone, so it runs first
and unconditionally: a dead status panel or an all-dead fleet must not stop the picker
from shedding rows that are never a usable chat model.

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
  --no-hide-junk     keep non-chat junk rows (TTS/OCR/embedding/video/web tools)
  --timeout SEC      status panel and subprocess timeout (default 20)
  --dry-run          report only, change nothing

Exit codes: 0 ok (or nothing to do), 2 bad usage, 3 status panel unreachable,
4 nothing verified as REAL (refuses to filter everything), 5 catalog problem.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request

BACKUP_TEMPLATE = ".bak-%Y%m%d-%H%M%S"
STATE_FILE = ".catalog-filter-restore-state.json"
STATE_COOLDOWN = 3600

# Rows that are never a usable chat model when picked in Codex. The substrings are
# only ones unique to the junk class, so real rows survive: "seedream" hides image
# generation while seed-2.0-code stays, "ocr" hides glm-ocr while qwen3-vl-plus stays,
# and the -i2v/-r2v/-t2v video tags never appear in a text model slug.
JUNK_SUBSTRINGS = (
    "-i2v", "-r2v", "-t2v",                              # image / video generation
    "tts", "speech", "seedream", "happyhorse", "-song",  # voice, music, video
    "embedding", "rerank",                              # retrieval utilities
    "ocr", "-asr",                                      # OCR, speech-to-text
    "web-search", "web-reader", "bocha",                # web-search tools
    "computer_use_subagent",                            # browser subagent
    "-official",                                        # "-Official" dual listings
    "cogevol", "spark-x2.5",                            # research/PPT agents, tiny spark models
)
# Substrings are only ones unique to the junk class, so real rows survive: "seedream"
# hides image generation while seed-2.0-code stays, "ocr" hides glm-ocr while
# qwen3-vl-plus stays, and -i2v/-r2v/-t2v never appear in a text model slug.
JUNK_SLUGS = {
    "qoder/model",   # placeholder row ocx advertises, never a real model
}
# A trailing -MMDD date marks a snapshot that merely duplicates a live sibling
# (deepseek-v4-flash-0731 beside deepseek-v4-flash), so it is safe to hide; no real
# model slug ends in four digits after a dash. Slugs match case-insensitively, so
# precompute the lowercased set and the dated-suffix regex once.
_JUNK_SLUGS_LOWER = frozenset(s.lower() for s in JUNK_SLUGS)
_DATED_RE = re.compile(r"-\d{4}$")


def is_junk(slug):
    """True when a catalog slug is a non-chat utility or placeholder row."""
    low = slug.lower()
    return (low in _JUNK_SLUGS_LOWER
            or any(token in low for token in JUNK_SUBSTRINGS))


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


def probe_native_pool(proxy_base, timeout):
    """True when the local proxy's native (account-pool) provider can still serve.

    The native rows in the Codex picker have no slash prefix, so no bridge verdict
    covers them. When the account pool behind them is empty they all fail with
    "OpenAI account pool has no usable account credential" at request time, which is
    the worst failure mode for a picker: the option looks available and only breaks
    after the user commits to it.

    Returns (ok, detail). Never raises; an unknown pool is reported as usable so a
    probe failure cannot silently empty the picker.
    """
    base = (proxy_base or "").rstrip("/")
    if not base:
        return True, "no proxy base configured"
    body = json.dumps({
        "model": "gpt-5.5",
        "input": "ping",
        "max_output_tokens": 16,
    }).encode("utf-8")
    request = urllib.request.Request(
        base + "/v1/responses", data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer sk-local"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return (200 <= response.status < 300), "HTTP %s" % response.status
    except urllib.error.HTTPError as exc:
        try:
            payload = exc.read().decode("utf-8", "replace")[:200]
        except Exception:
            payload = ""
        text = (payload or "").lower()
        if exc.code == 401 and "no usable account credential" in text:
            return False, "account pool has no usable credential (HTTP 401)"
        # Any other status is a probe we cannot read as "pool down"; stay permissive.
        return True, "HTTP %s %s" % (exc.code, payload[:120])
    except Exception as exc:
        # Unreachable proxy means the whole fleet is down anyway; do not act on it.
        return True, "%s: %s" % (type(exc).__name__, exc)


def drop_junk(models):
    """Return (keepers, dropped) after hiding junk rows, dropped keyed by provider."""
    # A dated snapshot (trailing -MMDD) is only redundant when its plain sibling is
    # also advertised, so lone dated-named models like qwen3-30b-a3b-instruct-2507 stay.
    advertised = {(m.get("slug") or m.get("id") or m.get("model") or "").lower()
                  for m in models if isinstance(m, dict)}

    def is_dated_snapshot(low):
        base = _DATED_RE.sub("", low)
        return base != low and base in advertised

    keepers, dropped = [], {}
    for model in models:
        slug = model.get("slug") or model.get("id") or model.get("model") or ""
        low = slug.lower()
        provider = slug.split("/", 1)[0] if "/" in slug else "<native>"
        if is_junk(slug) or is_dated_snapshot(low):
            dropped.setdefault(provider, []).append(slug)
        else:
            keepers.append(model)
    return keepers, dropped


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


def write_catalog(path, data, keepers, summary, no_backup):
    """Back up, atomically replace the catalog with keepers, then verify.

    Re-serialising must round-trip byte for byte, or the write would reformat a file
    that both Codex and CC Switch read. Returns True on success; on any failure sets
    summary["error"] and returns False without leaving a partial file.
    """
    with open(path, encoding="utf-8") as fh:
        original = fh.read()
    try:
        proof = json.dumps(load_catalog(path), indent=2, ensure_ascii=False) + "\n"
    except Exception as exc:
        summary["error"] = "cannot prove round-trip (%s)" % exc
        return False
    if proof != original:
        summary["error"] = "round-trip mismatch, refusing to rewrite %s" % path
        return False

    if not no_backup:
        backup = path + time.strftime(BACKUP_TEMPLATE)
        if os.path.exists(backup):
            backup = "%s-%d" % (backup, os.getpid())
        with open(path, "rb") as src, open(backup, "wb") as dst:
            dst.write(src.read())
        summary["backup"] = backup

    data["models"] = keepers
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
        summary["error"] = "write left an unreadable file: %s" % exc
        return False
    if len(after.get("models") or []) != len(keepers):
        print("catalog-filter: WRITE LEFT THE WRONG MODEL COUNT", file=sys.stderr)
        summary["error"] = "write left the wrong model count"
        return False
    summary["verified_after_write"] = True
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="hide catalog models whose bridge is not verified REAL and junk rows",
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
    parser.add_argument("--no-hide-junk", action="store_true",
                        help="keep non-chat junk rows (TTS/OCR/embedding/video/web tools)")
    parser.add_argument("--hide-native-when-pool-down", action="store_true",
                        help="hide the unprefixed native picker rows when the "
                             "local proxy reports its account pool has no usable "
                             "credential")
    parser.add_argument("--proxy-base", default=os.environ.get(
        "FLEET_PROXY_BASE", "http://127.0.0.1:10100"),
        help="local proxy base used for the native-pool probe")
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

    original_count = len(models)
    summary = {
        "catalog": path,
        "status_url": args.status_url,
        "verified_at": None,
        "bridges": [],
        "real": [],
        "keep": sorted(keep),
        "models_before": original_count,
    }

    # Hide junk rows first, before any bridge check: this cleanup is decided from the
    # slug alone and must not depend on the status panel or a verified-REAL fleet.
    junk_dropped = {}
    if not args.no_hide_junk:
        models, junk_dropped = drop_junk(models)
    summary["junk_removed"] = sum(len(v) for v in junk_dropped.values())
    summary["junk_removed_by_provider"] = {k: len(v)
                                           for k, v in sorted(junk_dropped.items())}
    summary["junk_removed_slugs"] = junk_dropped

    def finish(keepers, code):
        """Report counts, write unless a dry run, and return the run code."""
        summary["models_after"] = len(keepers)
        summary["removed"] = original_count - len(keepers)
        if args.dry_run:
            summary["dry_run"] = True
        elif len(keepers) != original_count:
            if not write_catalog(path, data, keepers, summary, args.no_backup):
                return 5
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return code

    try:
        status = fetch_status(args.status_url, args.timeout)
    except Exception as exc:
        # Junk cleanup is independent of the panel, so persist it even with the panel
        # down; only signal failure when there was nothing to remove either way.
        summary["status_error"] = "status panel unreachable: %s" % exc
        summary["bridge_filter"] = "skipped"
        return finish(models, 0 if summary["junk_removed"] else 3)

    bridges = {b.get("name") for b in (status.get("bridges") or [])
               if isinstance(b, dict) and b.get("name")}
    verify = status.get("verify") or {}
    real = {r for r in (verify.get("real") or []) if isinstance(r, str)}
    summary["verified_at"] = verify.get("generated_at")
    summary["bridges"] = sorted(bridges)
    summary["real"] = sorted(real)

    if not real:
        # Same as above: keep the junk cleanup, but do not touch bridge rows.
        summary["error"] = "no bridge verified REAL; refusing to filter bridges"
        return finish(models, 0 if summary["junk_removed"] else 4)

    present = {p for p in (provider_of(m) for m in models) if p}
    # A REAL provider with no rows left is how a recovered bridge stays invisible.
    suspects = sorted((real & bridges) - present - keep)
    if suspects and not args.dry_run and not args.no_restore:
        # ocx sync rewrites whatever catalog config.toml names, so restoring while
        # filtering some other file would corrupt the wrong one.
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
                    # ocx sync re-advertises every model, junk included, so the junk
                    # filter has to run again on the reloaded catalog.
                    if not args.no_hide_junk:
                        models, junk_dropped = drop_junk(models)
                        summary["junk_removed"] = sum(
                            len(v) for v in junk_dropped.values())
                        summary["junk_removed_slugs"] = junk_dropped
                    summary["restored"] = sorted(
                        ((real & bridges)
                         & {provider_of(m) for m in models}) - present)
                except Exception as exc:
                    return fail("catalog unreadable after ocx sync: %s" % exc, 5)

    # Only bridges can be judged: a provider with no bridge has no verdict to act on.
    candidates = bridges - keep
    if only:
        candidates &= only
    unavailable = sorted(candidates - real)
    summary["unavailable"] = unavailable

    native_down = False
    if args.hide_native_when_pool_down:
        pool_ok, detail = probe_native_pool(args.proxy_base, args.timeout)
        native_down = not pool_ok
        summary["native_pool"] = detail
        summary["native_pool_down"] = native_down

    keepers, dropped = [], {}
    for model in models:
        provider = provider_of(model)
        slug = model.get("slug") or model.get("id") or model.get("model") or "?"
        if provider is None:
            # No bridge owns a native row, so no verdict can reach it. Only the
            # explicit pool probe may hide it, and only while the pool is down.
            if native_down:
                dropped.setdefault("<native>", []).append(slug)
                continue
            keepers.append(model)
        elif provider in unavailable:
            dropped.setdefault(provider, []).append(slug)
        else:
            keepers.append(model)

    summary["removed_by_provider"] = {k: len(v) for k, v in sorted(dropped.items())}
    summary["removed_slugs"] = {k: v for k, v in sorted(dropped.items())}
    return finish(keepers, 0)


if __name__ == "__main__":
    sys.exit(main())

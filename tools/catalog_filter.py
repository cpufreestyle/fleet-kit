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
  --keep-backups N   keep only the newest N .bak-* files (default 5, -1 keeps all)
  --no-restore       do not run ocx sync to bring back a verified-REAL provider
  --no-hide-junk     keep non-chat junk rows (TTS/OCR/embedding/video/web tools)
  --timeout SEC      status panel and subprocess timeout (default 20)
  --max-verify-age SEC
                      refuse to drop bridge rows on a verdict snapshot older
                      than this (default 24h); junk rows are always shed
  --dry-run          report only, change nothing
  --report-only      same as --dry-run, kept for the shell wrapper

Exit codes: 0 ok (or nothing to do), 2 bad usage, 3 status panel unreachable,
4 nothing verified as REAL (refuses to filter everything),
5 catalog problem, 6 verdict snapshot too old to act on.
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request

# Everything this tool talks to is on loopback: the status panel, the bridge
# gateways, the native-row probe. urllib would otherwise follow the system
# proxy, which reroutes those requests through it and answers with its own
# verdicts -- a 503 of the proxy's read as a bridge failure, and a proxy that
# is down takes the whole filter with it. verify_real_calls.py and
# fleet_probe.py already build a proxy-free opener for the same reason.
NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# npm drops a shell script plus a .cmd/.ps1 pair next to ocx, and subprocess on
# Windows can only exec the .cmd, so resolve it through fleet_platform instead
# of relying on PATH spelling (see ocx_exe's own docstring).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fleet_platform import ocx_exe
from catalog_common import prune_backups  # noqa: F401  (re-exported for callers)

BACKUP_TEMPLATE = ".bak-%Y%m%d-%H%M%S"
STATE_FILE = ".catalog-filter-restore-state.json"
STATE_COOLDOWN = 3600
KEEP_BACKUPS = 5
# An old verdict is not allowed to delete rows, for the same reason
# catalog_sort.py refuses to let a stale reach snapshot drop them: bridges
# recover (a renewed token, a VPN back up, an upstream 503 clearing), and the
# prover only runs on demand, so the snapshot in the panel is routinely a day
# or two old. Verified bridges are 13, and the verdict list is 7 -- hiding the
# other six off a 3 day old verdict hides working models.
MAX_VERIFY_AGE = 86400

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
    with NO_PROXY.open(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))


def ocx(args, timeout):
    """Run an ocx subcommand; returns (ok, last-output-line)."""
    try:
        out = subprocess.run([ocx_exe()] + args, capture_output=True, timeout=timeout)
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


NATIVE_PROBE_MODELS = ("gpt-5.5", "step-3.7-flash", "gpt-5.6-luna")


def codex_provider_base(codex_home):
    """base_url of the provider Codex actually calls, without a trailing /v1.

    The native picker rows are sent to the model_provider named in config.toml,
    which on this install is the CC Switch gateway rather than the opencodex
    proxy, so probing the ocx port says nothing about whether a native row
    works. Returns None when the config does not name a usable provider.
    """
    try:
        with open(os.path.join(codex_home, "config.toml"),
                  encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None
    provider = re.search(r'^\s*model_provider\s*=\s*"([^"]+)"', text, re.M)
    if not provider:
        return None
    block = re.search(r'^\s*\[model_providers\.%s\]\s*$'
                      % re.escape(provider.group(1)), text, re.M)
    if not block:
        return None
    tail = text[block.end():]
    following = re.search(r"^\s*\[", tail, re.M)
    section = tail[:following.start()] if following else tail
    url = re.search(r'^\s*base_url\s*=\s*"([^"]+)"', section, re.M)
    if not url:
        return None
    base = url.group(1).rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return base or None


def probe_native_pool(proxy_base, timeout, models=None):
    """True when the gateway serving the native rows can still serve them.

    The native rows in the Codex picker have no slash prefix, so no bridge
    verdict covers them. When the account pool behind them is empty they all
    fail with "OpenAI account pool has no usable account credential" at request
    time, which is the worst failure mode for a picker: the option looks
    available and only breaks after the user commits to it.

    Returns (ok, detail). Never raises; an unknown pool is reported as usable so
    a probe failure cannot silently empty the picker.

    The request has to be one the gateway accepts before it ever reaches the
    account pool: a list input and stream=true, or the gateway answers 400
    "Input must be a list" / "Stream must be set to true" and the 401 this probe
    exists to see is never emitted. A model name the gateway does not know (404)
    says nothing about the pool, so the candidates are tried in turn.
    """
    base = (proxy_base or "").rstrip("/")
    if not base:
        return True, "no proxy base configured"
    candidates = [m for m in (models or ()) if m] or list(NATIVE_PROBE_MODELS)
    last = ""
    for model in candidates:
        body = json.dumps({
            "model": model,
            "input": [{"role": "user",
                       "content": [{"type": "input_text", "text": "ping"}]}],
            "max_output_tokens": 16,
            "stream": True,
        }).encode("utf-8")
        request = urllib.request.Request(
            base + "/v1/responses", data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer sk-local"},
        )
        try:
            with NO_PROXY.open(request, timeout=timeout) as response:
                return True, "HTTP %s via %s" % (response.status, model)
        except urllib.error.HTTPError as exc:
            try:
                payload = exc.read().decode("utf-8", "replace")[:200]
            except Exception:
                payload = ""
            text = (payload or "").lower()
            if exc.code == 401 and "no usable account credential" in text:
                return (False, "account pool has no usable credential "
                               "(HTTP 401 via %s)" % model)
            # Any other status is a probe we cannot read as "pool down"; stay
            # permissive and let the next candidate speak for the pool.
            last = "HTTP %s via %s: %s" % (exc.code, model, payload[:100])
        except Exception as exc:
            # Unreachable proxy means the whole fleet is down anyway.
            return True, "%s: %s" % (type(exc).__name__, exc)
    return True, last or "no candidate model accepted"


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


def verify_age_seconds(verify):
    """Age of the verdict snapshot in seconds, or None when undatable.

    Unlike catalog_sort.snapshot_age_seconds, a naive timestamp is read as
    local time: the snapshot comes from the status panel, whose now_str()
    writes naive local time, while the reach snapshot catalog_sort consumes
    carries its own offset.
    """
    stamp = verify.get("generated_at")
    if not stamp:
        return None
    try:
        when = datetime.datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.datetime.now().astimezone().tzinfo)
    return max(0.0, (datetime.datetime.now(when.tzinfo) - when).total_seconds())


HIDDEN_STATE_FILE = ".catalog-filter-hidden.json"


def hidden_count_path(catalog):
    return os.path.join(os.path.dirname(os.path.abspath(catalog)),
                        HIDDEN_STATE_FILE)


def record_hidden_count(catalog, keepers, status, real):
    """Write down how many slash rows the catalog is short of the fleet.

    ocx-catalog-guard heals a catalog that fell below MIN_MODELS bridge rows,
    which is right when the catalog was stripped (CC Switch regenerating it)
    and wrong when this filter hid those rows for bridges verified not REAL:
    the guard would re-add every broken row and the two 300s timers would undo
    each other for as long as the bridges stay down. It needs the shortfall
    this filter explains, so it is computed here, where the panel answer is
    already in hand.

    Counts, not slugs: a bridge advertises "gpt-6-astra" while the catalog
    stores "workbuddy-gpt/gpt-6-astra", and cline advertises
    "cline-free/..." against a "cline/cline-free/..." row, so predicting a
    slug means re-implementing every bridge's aliasing. Comparing a bridge's
    own model count with the rows that survived for it needs none of that.

    A shortfall only counts while the working bridges still hold their rows.
    A catalog that lost those too was stripped, and healing that is the
    guard's whole job, so it is reported as no shortfall at all.
    """
    rows = {}
    for model in keepers:
        if not isinstance(model, dict):
            continue
        slug = model.get("slug") or model.get("id") or model.get("model") or ""
        if "/" in slug:
            provider = slug.split("/", 1)[0]
            rows[provider] = rows.get(provider, 0) + 1
    real_rows = sum(rows.get(name, 0) for name in (real or ()))
    hidden = 0
    if real_rows:
        for bridge in status.get("bridges") or []:
            if not isinstance(bridge, dict):
                continue
            name = bridge.get("name")
            advertised = (bridge.get("probe") or {}).get("count")
            if not name or not isinstance(advertised, int):
                continue
            hidden += max(0, advertised - rows.get(name, 0))
    payload = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "slash_rows_hidden": hidden,
               "slash_rows_in_catalog": sum(rows.values()),
               "real_slash_rows": real_rows}
    path = hidden_count_path(catalog)
    try:
        handle, tmp = tempfile.mkstemp(prefix=".catalog-filter-hidden-",
                                       dir=os.path.dirname(path))
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except OSError as exc:
        print("catalog-filter: cannot record hidden rows (%s)" % exc,
              file=sys.stderr)
    return hidden


def read_hidden_count(catalog):
    """Slash rows the filter reported missing, 0 when that is unknown."""
    try:
        with open(hidden_count_path(catalog), encoding="utf-8") as fh:
            data = json.load(fh)
        return int(data.get("slash_rows_hidden") or 0)
    except (OSError, ValueError, TypeError):
        return 0


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


# Every writer in this chain serialises the catalog its own way: catalog_sort.py
# writes json.dump(..., ensure_ascii=False, indent=1) with no trailing newline,
# ocx writes its own layout, and hand edits add whatever the editor preferred. So
# the round-trip proof probes the layouts that are actually in circulation instead
# of assuming one of them. Probing is what keeps the guard meaningful: the file is
# still refused unless some json.dumps call reproduces it byte for byte.
LAYOUT_INDENTS = (1, 2, 3, 4, 6, 8, "\t", None)


def detect_layout(text, data):
    """(dump kwargs, trailing bytes) that rebuild `text` from `data`, or None.

    Trailing bytes are kept apart because the catalog in the wild ends with no
    newline at all, and appending one would still parse fine but would show up
    as a diff on every read.
    """
    tail = ""
    body = text
    if body.endswith("\r\n"):
        tail, body = "\r\n", body[:-2]
    elif body.endswith("\n"):
        tail, body = "\n", body[:-1]
    for ensure_ascii in (False, True):
        for indent in LAYOUT_INDENTS:
            try:
                proof = json.dumps(data, ensure_ascii=ensure_ascii, indent=indent)
            except (TypeError, ValueError):
                continue
            if proof == body:
                return {"ensure_ascii": ensure_ascii, "indent": indent}, tail
    return None


def write_catalog(path, data, keepers, summary, no_backup,
                  keep_backups=KEEP_BACKUPS):
    """Back up, atomically replace the catalog with keepers, then verify.

    Re-serialising must round-trip byte for byte, or the write would reformat a file
    that both Codex and CC Switch read. Returns True on success; on any failure sets
    summary["error"], says why on stderr, and returns False without leaving a partial
    file.
    """
    with open(path, encoding="utf-8") as fh:
        original = fh.read()
    try:
        fresh = load_catalog(path)
    except Exception as exc:
        summary["error"] = "cannot prove round-trip (%s)" % exc
        print("catalog-filter: %s" % summary["error"], file=sys.stderr)
        return False
    if fresh != data:
        summary["error"] = "catalog changed on disk since it was read"
        print("catalog-filter: %s; not writing %s" % (summary["error"], path),
              file=sys.stderr)
        return False
    layout = detect_layout(original, fresh)
    if layout is None:
        summary["error"] = ("unrecognised catalog layout, refusing to rewrite %s"
                            % path)
        print("catalog-filter: refusing to rewrite %s -- no json.dumps layout "
              "(indent / trailing newline / ensure_ascii) reproduces the file "
              "byte for byte, so a rewrite would reformat a file both Codex and "
              "CC Switch read" % path, file=sys.stderr)
        return False
    dump_kwargs, tail = layout

    if not no_backup:
        backup = path + time.strftime(BACKUP_TEMPLATE)
        if os.path.exists(backup):
            backup = "%s-%d" % (backup, os.getpid())
        with open(path, "rb") as src, open(backup, "wb") as dst:
            dst.write(src.read())
        summary["backup"] = backup

    data["models"] = keepers
    payload = json.dumps(data, **dump_kwargs) + tail
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
    pruned = prune_backups(path, keep_backups)
    if pruned:
        summary["backups_pruned"] = [os.path.basename(p) for p in pruned]
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
    parser.add_argument("--keep-backups", type=int, default=int(
        os.environ.get("FLEET_KEEP_BACKUPS", KEEP_BACKUPS)),
        help="keep only the newest N catalog .bak-* files (-1 keeps all)")
    parser.add_argument("--no-restore", action="store_true")
    parser.add_argument("--restore-cooldown", type=int, default=STATE_COOLDOWN)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--max-verify-age", type=float, default=float(
        os.environ.get("FLEET_MAX_VERIFY_AGE", MAX_VERIFY_AGE)),
        help="refuse to drop bridge rows on a verdict snapshot older than "
             "this many seconds (-1 trusts any age)")
    parser.add_argument("--no-hide-junk", action="store_true",
                        help="keep non-chat junk rows (TTS/OCR/embedding/video/web tools)")
    parser.add_argument("--hide-native-when-pool-down", action="store_true",
                        help="hide the unprefixed native picker rows when the "
                             "local proxy reports its account pool has no usable "
                             "credential")
    parser.add_argument("--proxy-base", default=os.environ.get(
        "FLEET_PROXY_BASE", "http://127.0.0.1:10100"),
        help="base used for the native-pool probe; unset means the "
             "model_provider base_url from config.toml, falling back to this "
             "default")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report-only", action="store_true",
                        help="report what would be dropped, change nothing")
    args = parser.parse_args(argv)

    # FleetKit convention: the picker shows only models that really connect.
    # A plain run rewrites the catalog to drop unreachable providers; pass
    # --report-only to just print what would be dropped.
    if args.report_only:
        args.dry_run = True
        args.no_restore = True
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

    # Filled in once the panel answer is in hand; finish() reports the
    # shortfall the guard needs from it, and only then.
    panel = None

    def finish(keepers, code):
        """Report counts, write unless a dry run, and return the run code."""
        summary["models_after"] = len(keepers)
        summary["removed"] = original_count - len(keepers)
        if args.dry_run:
            summary["dry_run"] = True
        else:
            if len(keepers) != original_count:
                if not write_catalog(path, data, keepers, summary,
                                     args.no_backup, args.keep_backups):
                    print(json.dumps(summary, indent=2, ensure_ascii=False))
                    return 5
            # Every run, not only the runs that remove something: a catalog
            # that is already filtered is exactly the state the guard timer
            # needs explained, and the count is re-derived from the panel each
            # time so nothing has to be seeded or reconciled by hand.
            if panel is not None:
                summary["slash_rows_hidden"] = record_hidden_count(
                    path, keepers, panel["status"], panel["real"])
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
    panel = {"status": status, "real": real}
    summary["verified_at"] = verify.get("generated_at")
    summary["bridges"] = sorted(bridges)
    summary["real"] = sorted(real)

    if not real:
        # Same as above: keep the junk cleanup, but do not touch bridge rows.
        summary["error"] = "no bridge verified REAL; refusing to filter bridges"
        return finish(models, 0 if summary["junk_removed"] else 4)

    # A verdict is only evidence about the moment it was taken. Refuse to
    # delete rows off an old one before any side effect, including the
    # restore sync below.
    age = verify_age_seconds(verify)
    summary["verify_age_seconds"] = None if age is None else round(age, 1)
    stale = age is None or (args.max_verify_age >= 0
                            and age > args.max_verify_age)
    if stale:
        summary["error"] = ("verify snapshot is %s (max %ss), too old to "
                            "filter bridges"
                            % ("undated" if age is None else "%.0fs old" % age,
                               args.max_verify_age))
        summary["bridge_filter"] = "skipped (stale verdict)"
        return finish(models, 0 if summary["junk_removed"] else 6)

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
        # Probe the gateway Codex actually calls for unprefixed rows -- the
        # provider config.toml names -- not whichever proxy port is the default.
        proxy_base = args.proxy_base
        if "FLEET_PROXY_BASE" not in os.environ:
            configured = codex_provider_base(args.codex_home)
            if configured:
                proxy_base = configured
        candidates = [m.get("slug") or m.get("id") or m.get("model") or ""
                      for m in models if provider_of(m) is None]
        pool_ok, detail = probe_native_pool(proxy_base, args.timeout, candidates)
        native_down = not pool_ok
        summary["proxy_base"] = proxy_base
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

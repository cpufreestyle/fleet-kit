#!/usr/bin/env python3
"""Re-point Codex at the FleetKit gateway after a provider switcher takes the file.

A third-party provider switcher (CC Switch on this machine) owns
~/.codex/config.toml and rewrites it on every provider switch. Measured
2026-09-30 11:20: the switcher replaced a healthy FleetKit config with its own,
dropping model_catalog_json, openai_base_url and the
[model_providers.opencodex] table, and setting model_provider = "custom", so
every Codex session opened on the switcher's upstream instead of the gateway.
Nothing complained at that moment -- the picker still listed the fleet bridge
models -- but picking one sent the fleet model name to the foreign upstream,
which answered 404 "model does not exist" (lingxi/lingxi-deepseek-flash against
api.stepfun.com/step_plan/v1/responses). The same rewrite also hid the fleet
models, because the catalog was no longer declared.

This is the FleetKit half of tools/pin_shim_base_url.py, in the same shape: an
idempotent, surgical pin that is safe to run from a timer.

  * it fires only when the fleet markers are gone, which is the switcher's
    signature. A config that still declares the catalog and the gateway is
    left byte-for-byte alone, so an operator who deliberately aims Codex at
    another provider is not fought;
  * it repairs only FleetKit's own keys: model_catalog_json, openai_base_url,
    experimental_realtime_ws_base_url, [model_providers.opencodex] and -- once
    the file has clearly been taken over -- the top-level model_provider.
    Foreign sections, [model_providers.custom] above all, are copied through
    untouched, so sessions already open on the switcher's provider keep
    working;
  * the default model is replaced only when the pinned route cannot serve it,
    judged against the very catalog the pin declares, and only with a model
    that catalog actually lists;
  * the write is atomic and re-reads the file immediately before the replace,
    because the switcher rewrites the same file on its own schedule.

Run by hand (--dry-run to preview) or from tools/ocx-catalog-guard.sh, which
already wakes every 300s to keep the fleet models selectable.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

FLEET_PROVIDER = "opencodex"
GATEWAY = os.environ.get("FLEET_GATEWAY") or "http://127.0.0.1:10100/v1"
CATALOG_NAME = os.environ.get("FLEET_CATALOG_NAME") or "opencodex-catalog.json"
FALLBACK_MODEL = "combo/fleetcore"

# experimental_realtime_ws_base_url is deliberately NOT pinned: the desktop
# app prefers that WebSocket transport when the key exists, and this fleet's
# opencodex answers the upgrade with 426 -- every compose then locks (measured
# 2026-10-04: all turns served over the plain HTTP responses API). Leave the
# key out of the config entirely.
ROOT_KEYS = ("model_catalog_json", "openai_base_url")
# experimental_bearer_token: ocx's own rewrite of the provider table drops it,
# and without it requires_openai_auth=true leaves the desktop app unable to
# authenticate the provider -- the compose/send control greys out (measured
# 2026-10-04). Re-pinned to the proxy-managed placeholder on every repair.
PROVIDER_KEYS = ("base_url", "wire_api", "requires_openai_auth",
                 "experimental_bearer_token")


def _key(raw):
    """The key a `key = value` line carries, or "" for anything else."""
    stripped = raw.strip()
    if ("=" not in stripped or stripped.startswith("[")
            or stripped.startswith("#")):
        return ""
    return stripped.split("=", 1)[0].strip()


def _toml_str(value):
    """A TOML basic string for `value`, escaped so a Windows path survives."""
    return '"%s"' % value.replace("\\", "\\\\").replace('"', '\\"')


def _value(raw):
    """The TOML value of a line, unescaped and unquoted."""
    val = raw.split("=", 1)[1].strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
        val = val[1:-1]
        if val.startswith("\\") or "\\\\" in val:
            val = val.replace("\\\\", "\x00").replace("\\", "")\
                     .replace("\x00", "\\")
    return val


def _root_line(key, value):
    return "%s = %s\n" % (key, _toml_str(value))


def _prov_line(key, gateway):
    if key == "base_url":
        return "base_url = %s\n" % _toml_str(gateway)
    if key == "wire_api":
        # responses: Codex 26.930 dropped wire_api = "chat" entirely (config
        # load fails, discussion/7782) -- responses is the only supported wire,
        # and FreeLLMAPI now accepts the full Responses payload.
        return 'wire_api = "responses"\n'
    if key == "experimental_bearer_token":
        return 'experimental_bearer_token = "PROXY_MANAGED"\n'
    return "requires_openai_auth = false\n"


def _native(path):
    """A path as a Windows program spells it.

    A launchd-style caller reaches this tool with an MSYS path
    (/c/Users/me/.codex), and the MSYS runtime hands it over with forward
    slashes (C:/Users/me/.codex). Windows opens that too, but a config written
    that way is a config that only ever worked by accident, and the fleet
    catalog is exactly the line nobody re-checks. Write the backslash form.
    A POSIX path (/Users/me/.codex on the mac running the timer) is left
    alone: converting its slashes points the declaration at a file that
    does not exist, which is how the pin once repainted a Linux catalog
    line as escaped backslashes.
    """
    msys = re.match(r"^/([A-Za-z])(?=/|$)", path)
    if msys:
        return "%s:%s" % (msys.group(1).upper(), path[2:].replace("/", "\\"))
    if re.match(r"^[A-Za-z]:", path):
        return path.replace("/", "\\")
    return path


def _is_header(raw):
    stripped = raw.strip()
    return stripped.startswith("[") and stripped.endswith("]")


def root_items(lines):
    """(index, key, value) for every key declared at the top level.

    A switch can leave a stray `model_catalog_json` inside some other table;
    that is not a declaration, and treating it as one is how a pin ends up
    believing a config is healthy when the picker has no catalog at all.
    """
    items = []
    section = None
    for idx, raw in enumerate(lines):
        if _is_header(raw):
            section = raw.strip()[1:-1].strip()
            continue
        if section is None:
            key = _key(raw)
            if key:
                items.append((idx, key, _value(raw)))
    return items


def root_value(lines, key, default=""):
    for _, name, value in root_items(lines):
        if name == key:
            return value
    return default


def has_root_key(lines, key):
    return any(name == key for _, name, _ in root_items(lines))


def declared_catalog(lines, home):
    """The catalog model_catalog_json names, or the default one in `home`.

    A declared path that exists wins: it is the catalog the picker reads, so
    judging models against anything else would rate a working setup broken.
    """
    declared = root_value(lines, "model_catalog_json")
    if declared and os.path.isfile(declared):
        return declared
    return _native(os.path.join(_native(home), CATALOG_NAME))


def catalog_slugs(path):
    """The model slugs a catalog offers, for judging whether a default serves."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return set()
    rows = data.get("models") if isinstance(data, dict) else data
    slugs = set()
    for row in rows or []:
        if isinstance(row, dict):
            slug = row.get("slug") or row.get("id") or row.get("model") or ""
        else:
            slug = row or ""
        if slug:
            slugs.add(slug)
    return slugs


def missing_markers(lines, catalog_path, gateway, provider=FLEET_PROVIDER):
    """FleetKit keys the file no longer carries, or carries pointed elsewhere.

    The root keys alone are not the whole route: a config can keep every one
    of them and still have lost the provider table they route through, which
    is exactly how a thread pinned to that provider starts failing to load.
    So the table is checked too. Empty means "the fleet route is still
    declared here", which is the one state this pin refuses to touch.
    """
    missing = []
    declared = root_value(lines, "model_catalog_json")
    if not declared:
        missing.append("model_catalog_json")
    elif not os.path.isfile(declared):
        missing.append("model_catalog_json -> %s is not on disk" % declared)
    elif os.path.normcase(os.path.normpath(declared)) != \
            os.path.normcase(os.path.normpath(catalog_path)):
        missing.append("model_catalog_json -> %s" % declared)
    for key, want in (("openai_base_url", gateway),
                      ("experimental_realtime_ws_base_url", gateway)):
        if root_value(lines, key) != want:
            missing.append(key)
    if not provider_section_present(lines, provider):
        missing.append("model_providers.%s" % provider)
    return missing


def provider_section_present(lines, provider):
    header = "[model_providers.%s]" % provider
    return any(raw.strip() == header for raw in lines)


def rewrite(text, provider=FLEET_PROVIDER, catalog_path=CATALOG_NAME,
            gateway=GATEWAY, model="", pin_provider=True):
    """Return (new text, keys touched) with the fleet route pinned.

    Pure. Only the keys this pin owns are rewritten, and a key that already
    holds the wanted value is copied through verbatim, so a pinned config
    comes back byte-for-byte identical on the next run.
    """
    wanted_root = {"model_catalog_json": catalog_path,
                   "openai_base_url": gateway,
                   "experimental_realtime_ws_base_url": gateway}
    target = "model_providers.%s" % provider
    touched = []
    out = []
    section = None
    first_root_at = None
    lines = text.splitlines(keepends=True)
    bom = "\ufeff" if text.startswith("\ufeff") else ""
    i = 0
    while i < len(lines):
        raw = lines[i]
        if raw.strip() == "[%s]" % target:
            # Copy the section: repair the keys this pin owns in place and
            # append the ones the switcher dropped.
            out.append(raw)
            j = i + 1
            seen = set()
            while j < len(lines) and not _is_header(lines[j]):
                key = _key(lines[j])
                if key in PROVIDER_KEYS:
                    seen.add(key)
                    out.append(_prov_line(key, gateway))
                    touched.append("%s.%s" % (target, key))
                else:
                    out.append(lines[j])
                j += 1
            for key in PROVIDER_KEYS:
                if key not in seen:
                    out.append(_prov_line(key, gateway))
                    touched.append("%s.%s (added)" % (target, key))
            section = target
            i = j
            continue
        if _is_header(raw):
            section = raw.strip()[1:-1].strip()
            out.append(raw)
            i += 1
            continue
        key = _key(raw)
        if section is None and key:
            if first_root_at is None:
                first_root_at = len(out)
            if key == "model_provider":
                if pin_provider and _value(raw) != provider:
                    out.append("model_provider = %s\n" % _toml_str(provider))
                    touched.append("model_provider")
                else:
                    out.append(raw)
                i += 1
                continue
            if key == "model" and model and _value(raw) != model:
                out.append("model = %s\n" % _toml_str(model))
                touched.append("model")
                i += 1
                continue
            if key in ROOT_KEYS and _value(raw) != wanted_root[key]:
                out.append(_root_line(key, wanted_root[key]))
                touched.append(key)
                i += 1
                continue
        out.append(raw)
        i += 1

    # A key the file no longer has at all goes in front of the first root key,
    # or at the top when there is none: both stay inside the root table.
    add = []
    if pin_provider and not has_root_key(lines, "model_provider"):
        add.append("model_provider = %s\n" % _toml_str(provider))
    if model and not has_root_key(lines, "model"):
        add.append("model = %s\n" % _toml_str(model))
    for key in ROOT_KEYS:
        if not has_root_key(lines, key):
            add.append(_root_line(key, wanted_root[key]))
    if add:
        at = first_root_at if first_root_at is not None else len(bom)
        out[at:at] = add
        touched.extend(_key(item) for item in add)

    if not provider_section_present(lines, provider):
        if out and out[-1].strip():
            out.append("\n")
        out.append("[%s]\n" % target)
        out.append("name = %s\n" % _toml_str("FleetKit Gateway"))
        for key in PROVIDER_KEYS:
            out.append(_prov_line(key, gateway))
        touched.append("%s (added)" % target)

    return "".join(out), touched


def _atomic_write(path, text):
    """Replace the file in one step so no reader sees a partial config.

    Codex reads this file on every invocation, and a truncated config is a
    broken Codex rather than a merely un-pinned one.
    """
    tmp = "%s.fleetroute.tmp" % path
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def pin_once(path, codex_home=None, provider=FLEET_PROVIDER, gateway=GATEWAY,
             dry_run=False, pin_provider=True):
    """Pin exactly once. Returns (wrote or would write, human-readable detail).

    Nothing raises, so a caller on a timer logs the detail and carries on. The
    text is re-read immediately before the replace and the pin is abandoned
    when the file moved underneath us: the switcher writes this file too, and
    writing back a half-parsed config would break Codex outright.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            original = fh.read()
    except OSError as exc:
        return False, "cannot read %s: %s" % (path, exc)

    home = codex_home or os.path.dirname(os.path.abspath(path))
    lines = original.splitlines()
    catalog = declared_catalog(lines, home)
    missing = missing_markers(lines, catalog, gateway)
    current = root_value(lines, "model_provider")
    slugs = catalog_slugs(catalog)
    model = root_value(lines, "model")
    model_servable = bool(slugs) and model in slugs
    replacement = FALLBACK_MODEL if (slugs and FALLBACK_MODEL in slugs
                                     and not model_servable) else ""
    # A missing provider table is repaired on its own, but it never drags the
    # root model_provider along with it. absent model_provider is a legal
    # operator choice (Codex then uses its built-in default), not evidence
    # that the file was taken over, so only root keys justify rewriting it.
    root_missing = [m for m in missing if not m.startswith("model_providers.")]
    touch_provider = pin_provider and bool(root_missing)

    if missing:
        # The switcher's signature: something the route needs is gone or
        # points elsewhere, so take the route back.
        pass
    elif current and current != provider:
        return False, ("no change: %s declares the fleet route and opens on %s "
                       "on purpose" % (os.path.basename(path), current))
    elif not (replacement and model and not model_servable):
        return False, "no change: fleet route already pinned"

    new_text, touched = rewrite(original, provider=provider,
                                catalog_path=catalog, gateway=gateway,
                                model=replacement, pin_provider=touch_provider)
    if new_text == original:
        return False, "no change: nothing to rewrite"
    detail = "pinned fleet route to %s: %s" % (
        gateway, ", ".join(sorted(set(touched))))
    if dry_run:
        return True, "[dry-run] would write %s (%s)" % (path, detail)
    try:
        with open(path, encoding="utf-8") as fh:
            if fh.read() != original:
                return False, "skipped: %s changed while pinning" % path
    except OSError as exc:
        return False, "cannot re-read %s: %s" % (path, exc)
    try:
        _atomic_write(path, new_text)
    except OSError as exc:
        return False, "cannot write %s: %s" % (path, exc)
    return True, "%s -> %s" % (detail, path)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="restore the FleetKit route in config.toml after a "
                    "provider switcher rewrites it")
    ap.add_argument("--config", default=os.path.expanduser("~/.codex/config.toml"))
    ap.add_argument("--codex-home", default="",
                    help="codex home that holds the model catalog (default: "
                         "the directory holding --config)")
    ap.add_argument("--provider", default=FLEET_PROVIDER)
    ap.add_argument("--gateway", default=GATEWAY)
    ap.add_argument("--keep-model-provider", action="store_true",
                    help="never touch the top-level model_provider line")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    if not os.path.isfile(args.config):
        print("  [warn] %s not found; nothing to pin" % args.config,
              file=sys.stderr)
        return 0
    home = args.codex_home or os.path.dirname(os.path.abspath(args.config))
    changed, detail = pin_once(args.config, codex_home=home,
                               provider=args.provider, gateway=args.gateway,
                               dry_run=args.dry_run,
                               pin_provider=not args.keep_model_provider)
    print("  %s" % detail)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

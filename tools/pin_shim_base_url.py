#!/usr/bin/env python3
"""Re-point Codex's custom provider base_url at the StepFun image-cap shim.

tools/stepfun_image_shim.py sits in front of CC Switch (127.0.0.1:15721) and
caps photos before StepFun's Plan API hits its 70-image ceiling (see
tools/image_cap.py for the measurement and tools/stepfun_image_shim.py for the
service). For Codex to use the shim, the provider it opens on must point at the
shim's port, not CC Switch's. CC Switch owns ~/.codex/config.toml and rewrites
that base_url back to 15721 whenever the operator switches providers, so a setup
that installs the shim has to pin it forward again -- and so does every later
re-run of opencodex/setup-providers.sh.

This is that pin, as a standalone idempotent tool:

  * it reads the top-level model_provider name (default "custom") and only
    touches that provider's base_url -- never another provider, never the file's
    other 15721 references (e.g. ANTHROPIC_BASE_URL in some server's env);
  * it swaps 127.0.0.1:15721 for 127.0.0.1:15722 as a host:port substring, so
    the scheme and the /v1 path survive verbatim;
  * if the base_url already points at the shim, or at another host, the file is
    left byte-for-byte unchanged and it reports "no change" -- safe to run on
    every setup and safe to schedule.

Run by hand (tools/pin_shim_base_url.py --dry-run to preview), or wire one call
after the default-model pin block in opencodex/setup-providers.sh.

The pin is also a scheduled one: the image-cap shim re-runs it on a timer,
because CC Switch rewrites the base_url the moment the operator switches
providers and nobody re-runs setup by hand. Every entry point is therefore
idempotent and concurrency-safe -- pin_once() re-reads the file before it
writes and refuses to write back a config that moved underneath it.
"""
from __future__ import annotations

import argparse
import os
import sys


def find_provider(lines, default="custom"):
    """The provider Codex opens on, from the top-level model_provider line."""
    for raw in lines:
        line = raw.strip()
        if line.startswith("model_provider") and "=" in line:
            val = line.split("=", 1)[1].strip()
            if val and val[0] in "\"'":
                val = val[1:]
            if val and val[-1] in "\"'":
                val = val[:-1]
            if val:
                return val
    return default


def rewrite(text, provider, from_hostport, to_hostport):
    """Return (new text, count) with the provider's base_url repointed.

    Pure, so a caller can tell whether the file moved between this call and the
    write without parsing anything twice. Only a base_url inside the target
    provider's section is touched; a sibling provider and an
    ANTHROPIC_BASE_URL on the same port are left alone.
    """
    section_target = "[model_providers.%s]" % provider
    in_target = False
    changed = 0
    out = []
    for raw in text.splitlines(keepends=True):
        stripped = raw.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_target = (stripped == section_target)
            out.append(raw)
            continue
        if (in_target and stripped.startswith("base_url")
                and from_hostport in raw):
            new = raw.replace(from_hostport, to_hostport)
            if new != raw:
                out.append(new)
                changed += 1
                continue
        out.append(raw)

    return "".join(out), changed


def _atomic_write(path, text):
    """Replace the file in one step so no reader sees a partial config.

    Codex reads this file on every invocation, and a truncated config is a
    broken Codex rather than a merely un-pinned one.
    """
    tmp = "%s.fleetpin.tmp" % path
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def pin_once(path, provider=None, from_hostport="127.0.0.1:15721",
             to_hostport="127.0.0.1:15722", dry_run=False):
    """Pin exactly once. Returns (wrote or would write, human-readable detail).

    A timer calls this, so the write has to survive CC Switch writing the same
    file at the same instant: the text is re-read immediately before the
    replace and the pin is abandoned when the file moved underneath us, because
    writing back a half-parsed config would break Codex outright instead of
    merely leaving the shim unused. Every failure comes back as a detail
    string and a False -- nothing raises, so a caller on a timer can log and
    carry on.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            original = fh.read()
    except OSError as exc:
        return False, "cannot read %s: %s" % (path, exc)
    name = provider or find_provider(original.splitlines())
    new_text, changed = rewrite(original, name, from_hostport,
                                to_hostport)
    if not changed:
        return False, ("no change: %s base_url is not on %s (already pinned?)"
                       % (name, from_hostport))
    if dry_run:
        return True, "[dry-run] would write %s" % path
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
    return True, "pinned %s base_url: %s -> %s" % (name, from_hostport,
                                                   to_hostport)


def pin(path, provider, from_hostport, to_hostport, dry_run=False):
    """Printing wrapper over pin_once(), for a human running the tool."""
    changed, detail = pin_once(path, provider, from_hostport, to_hostport,
                               dry_run)
    print("  %s" % detail)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="point the codex custom base_url at the StepFun image-cap shim")
    ap.add_argument("--config", default=os.path.expanduser("~/.codex/config.toml"))
    ap.add_argument("--provider", default="",
                    help="override the model_provider name (default: auto-detect)")
    ap.add_argument("--from-hostport", default="127.0.0.1:15721")
    ap.add_argument("--to-hostport", default="127.0.0.1:15722")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    path = args.config
    if not os.path.isfile(path):
        print("  [warn] %s not found; nothing to pin" % path, file=sys.stderr)
        return 0
    with open(path, encoding="utf-8") as fh:
        lines = fh.readlines()
    provider = args.provider or find_provider(lines)
    return pin(path, provider, args.from_hostport, args.to_hostport, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())

"""Shared plumbing for the catalog tools (catalog_sort, catalog_filter).

The catalog is one file rewritten by two tools on a 5 minute timer, and both
started out carrying the same half-page of scaffolding: the atomic write, the
backup pruning, the "when was this last read" stamp. Keeping it here means a
fix to the write path lands in one place instead of one of the two.

Behaviour-preserving helpers only: everything a tool does differently (how it
orders rows, which rows it drops) stays in that tool.
"""
from __future__ import annotations

import json
import os
import tempfile


def read_json(path: str) -> dict:
    """dict at `path`, or {} when it is missing or unreadable.

    A half-written catalog is a normal state while a timer rewrites it, so an
    unreadable file reads as "nothing known yet" rather than an error.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_json(path: str, payload: dict) -> None:
    """Atomically replace `path` with `payload` (indented, sorted, trailing \\n)."""
    directory = os.path.dirname(path) or "."
    handle, tmp = tempfile.mkstemp(prefix=".catalog-write-", dir=directory)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def prune_backups(path: str, keep: int):
    """Keep the newest `keep` .bak-* files beside the catalog, delete the rest.

    The 5 minute timers rewrite the catalog all day, and each write leaves a
    timestamped backup. Without a bound those pile up forever (89 files,
    99MB in one measured case), so trim to the newest few after a write.
    `keep < 0` keeps everything.

    Returns the removed paths. An unreadable entry is skipped rather than
    aborting the whole prune: one file that vanished mid-walk must not leave
    the rest of the pile in place.
    """
    if keep < 0:
        return []
    directory = os.path.dirname(path) or "."
    base = os.path.basename(path) + ".bak-"
    found = []
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    for name in names:
        if not name.startswith(base):
            continue
        full = os.path.join(directory, name)
        try:
            found.append((os.path.getmtime(full), full))
        except OSError:
            continue
    found.sort(reverse=True)
    removed = []
    for _mtime, full in found[keep:]:
        try:
            os.unlink(full)
            removed.append(full)
        except OSError:
            continue
    return removed


def atomic_write_text(path: str, text: str, suffix: str) -> None:
    """Replace a text file in one step so no reader sees a partial config.

    Codex reads config.toml on every invocation, so a truncated config is a
    broken Codex rather than a merely un-pinned one: write to a sibling temp
    file, fsync it, then rename over the target. `suffix` names the temp file
    so a crash leaves something identifiable behind.
    """
    tmp = "%s.%s" % (path, suffix)
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)

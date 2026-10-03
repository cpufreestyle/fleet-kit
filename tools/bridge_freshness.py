#!/usr/bin/env python3
"""Bridge freshness guard: a process must be newer than everything it loaded.

Background (2026-09-30): a green /health does not prove the running bytes are
new. All 15 bridges on this host are launchd-resident; after editing sources
under bridges/, a process that was never restarted keeps running stale
bytecode. Two real incidents this round:

- runtime/bridges/xhx/usage_ledger.py mtime 22:10:36 was 60s NEWER than the
  xhx process start 22:09:36 (fixed with
  launchctl kickstart -k gui/501/com.local.xhx2codex).
- runtime/bridges/_common.py (upstream guard, 9 importers) landed at 01:26:22;
  only codely/cline/workbuddy/workbuddy-gpt were restarted (01:26:34, new code
  live), while qoder/qwen/lingxi/trae/zcode still ran processes from 18:38 to
  22:57, so root-cause-26 guard was not live in them at all.

Judging only by bridges/<name>/ itself under-reports. Code loads three ways:

1. own directory: the process command line contains bridges/<name>/ (most
   bridges).
2. sibling directory via sys.path: workbuddy-cn/gpt converter.py inserts the
   shared dir bridges/workbuddy/ into sys.path and then runs
   `from core import main`, so the sibling directory path never shows up in
   the command line.
3. root-level shared modules: bridges/_common.py and bridges/_platform.py are
   imported by ~10 bridges, so touching one file changes a whole fleet.

newest_loaded takes the max mtime across the three and compares it to the
process start time (30s clock-noise tolerance by default).

Usage:

    runtime/tools/bridge_freshness.py                 # exit 1 when STALE
    runtime/tools/bridge_freshness.py --home <root>   # explicit home
    runtime/tools/bridge_freshness.py --json

Exit codes: 0 = all fresh/INFO rows, 1 = at least one STALE, 2 = no home found.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime

CODE_SUFFIXES = (".py", ".sh")
SKIP_DIRS = {"__pycache__", "auths", "assets", "node_modules", ".git"}
TOLERANCE_SECONDS = 30.0
PS_FIELDS = 6  # pid + lstart (5 tokens)
LSTART_FORMAT = "%a %b %d %H:%M:%S %Y"
PATH_TOKENS = ("sys.path", "os.path", "Path(", "dirname", "realpath", "abspath")
IMPORT_RE = re.compile(r"^\s*(?:import|from)\s+([A-Za-z_][\w.]*)")


@dataclass(frozen=True)
class Proc:
    pid: int
    started: datetime
    command: str


@dataclass(frozen=True)
class Row:
    bridge: str
    pid: str
    started: str
    status: str  # OK / STALE / INFO
    lag: str
    newest: str
    note: str


def source_files(path):
    """All code files under a directory (sorted, SKIP_DIRS pruned)."""
    out = []
    if not os.path.isdir(path):
        return out
    for root, dirs, files in os.walk(path):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for fname in sorted(files):
            if fname.endswith(CODE_SUFFIXES):
                out.append(os.path.join(root, fname))
    return out


def newest_code(path):
    """Latest code file in a directory as (mtime, filename) or (None, None)."""
    best = (None, None)
    for full in source_files(path):
        try:
            mtime = os.path.getmtime(full)
        except OSError:
            continue
        if best[0] is None or mtime > best[0]:
            best = (mtime, os.path.relpath(full, path))
    return best


def bridge_dirs(home):
    """Names of all bridge directories under <home>/bridges (sorted)."""
    bridges = os.path.join(home, "bridges")
    if not os.path.isdir(bridges):
        return []
    return sorted(
        name
        for name in os.listdir(bridges)
        if name not in SKIP_DIRS
        and os.path.isdir(os.path.join(bridges, name))
    )


def shared_modules(bridges):
    """Root-level shared modules of <bridges> as {module name: path}."""
    if not os.path.isdir(bridges):
        return {}
    return {
        fname[:-3]: os.path.join(bridges, fname)
        for fname in sorted(os.listdir(bridges))
        if fname.endswith(".py")
    }


def _iter_source_lines(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                yield line
    except OSError:
        return


def imported_modules(paths):
    """Top-level module names imported by these files."""
    found = set()
    for path in paths:
        for line in _iter_source_lines(path):
            match = IMPORT_RE.match(line)
            if match:
                found.add(match.group(1).split(".")[0])
    return found


def referenced_siblings(paths, own, siblings):
    """Sibling bridges whose code this directory pulls in via sys.path."""
    hits = set()
    for path in paths:
        for line in _iter_source_lines(path):
            for sib in siblings:
                if sib == own or sib in hits:
                    continue
                quoted = ('"%s"' % sib, "'%s'" % sib)
                if any(q in line for q in quoted) and any(
                    tok in line for tok in PATH_TOKENS
                ):
                    hits.add(sib)
    return hits


def parse_ps(text):
    """Parse `ps -ewwo pid,lstart,command` output into Proc records."""
    procs = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) <= PS_FIELDS:
            continue
        try:
            pid = int(parts[0])
            started = datetime.strptime(
                " ".join(parts[1:PS_FIELDS]), LSTART_FORMAT
            )
        except ValueError:
            continue
        procs.append(
            Proc(pid=pid, started=started, command=" ".join(parts[PS_FIELDS:]))
        )
    return procs


def read_ps():
    """Read the live process table."""
    out = subprocess.check_output(
        ["ps", "-ewwo", "pid,lstart,command"], text=True
    )
    return parse_ps(out)


def candidate_homes(tools_dir):
    """Default home candidates: [<suite>/runtime, <suite>]."""
    suite = os.path.dirname(os.path.abspath(tools_dir))
    return [
        os.path.normpath(os.path.join(suite, "runtime")),
        os.path.normpath(suite),
    ]


def resolve_home(tools_dir, procs):
    """Pick the candidate whose bridges/ directory runs the most processes."""
    best, best_score = None, -1
    for cand in candidate_homes(tools_dir):
        if not os.path.isdir(os.path.join(cand, "bridges")):
            continue
        marker = os.path.join(cand, "bridges") + os.sep
        score = sum(1 for proc in procs if marker in proc.command)
        if score > best_score:
            best, best_score = cand, score
    return best


def newest_loaded(bridges, own_dir, own, siblings):
    """Newest code a process started from own_dir really loads.

    Returns (mtime, filename, origin) with origin in own/sibling/shared.
    """
    entries = []
    mtime, rel = newest_code(own_dir)
    if mtime is not None:
        entries.append((mtime, rel, "own"))
    files = source_files(own_dir)
    for sib in referenced_siblings(files, own, siblings):
        sib_mtime, sib_rel = newest_code(os.path.join(bridges, sib))
        if sib_mtime is not None:
            entries.append((sib_mtime, sib + "/" + sib_rel, "sibling"))
    shared = shared_modules(bridges)
    for mod in sorted(imported_modules(files) & set(shared)):
        try:
            shared_mtime = os.path.getmtime(shared[mod])
        except OSError:
            continue
        entries.append((shared_mtime, mod + ".py", "shared"))
    if not entries:
        return (None, None, "own")
    return max(entries, key=lambda item: item[0])


def _lag_text(seconds):
    sign = "-" if seconds < 0 else ""
    seconds = abs(int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return "%s%dh%02dm" % (sign, hours, minutes)
    if minutes:
        return "%s%dm%02ds" % (sign, minutes, secs)
    return "%s%ds" % (sign, seconds)


def _file_time(mtime):
    if mtime is None:
        return "-"
    return datetime.fromtimestamp(mtime).strftime("%m-%d %H:%M:%S")


def _command_has_marker(command, marker):
    """Marker containment that survives separator and case differences.

    A bridge command line may carry either separator (a bash-launched bridge on
    Windows passes forward slashes while os.path.join emits backslashes), and
    Windows paths match case-insensitively.
    """
    command = command.replace(os.sep, "/")
    marker = marker.replace(os.sep, "/")
    if os.name == "nt":
        command, marker = command.lower(), marker.lower()
    return marker in command


def assess(home, procs, tolerance=TOLERANCE_SECONDS):
    """Assess every bridge directory under home; returns sorted Rows."""
    bridges = os.path.join(home, "bridges")
    siblings = set(bridge_dirs(home))
    rows = []
    for name in sorted(siblings):
        own_dir = os.path.join(bridges, name)
        marker = os.path.join(bridges, name) + os.sep
        matched = [proc for proc in procs
                   if _command_has_marker(proc.command, marker)]
        mtime, rel, origin = newest_loaded(bridges, own_dir, name, siblings)
        refs = referenced_siblings(source_files(own_dir), name, siblings)
        if mtime is not None and origin == "own":
            note = rel
        elif mtime is not None:
            note = "%s (%s)" % (rel, origin)
        else:
            note = "no code"
        if not matched:
            note = ("loaded by %s" % ", ".join(sorted(refs))) if refs \
                else "no process"
            rows.append(Row(name, "-", "-", "INFO", "-", _file_time(mtime), note))
            continue
        proc = matched[0]
        started_text = proc.started.strftime("%m-%d %H:%M:%S")
        if mtime is None:
            rows.append(Row(name, str(proc.pid), started_text, "INFO",
                            "-", "-", note))
            continue
        lag = mtime - proc.started.timestamp()
        status = "STALE" if lag > tolerance else "OK"
        if status == "STALE":
            note = "%s +%s" % (note, _lag_text(lag))
        rows.append(Row(name, str(proc.pid), started_text, status,
                        _lag_text(lag), _file_time(mtime), note))
    return sorted(rows, key=lambda row: row.bridge)


def render(rows):
    """Human-readable table plus summary line."""
    headers = ("BRIDGE", "PID", "STARTED", "STATUS", "LAG", "NEWEST", "CODE/NOTE")
    widths = [len(h) for h in headers]
    body = []
    for row in rows:
        cells = (row.bridge, row.pid, row.started, row.status, row.lag,
                 row.newest, row.note)
        for i, cell in enumerate(cells):
            widths[i] = max(widths[i], len(cell))
        body.append(cells)
    template = "  ".join("%%-%ds" % w for w in widths)
    lines = [template % headers]
    lines.extend(template % cells for cells in body)
    stale = [row.bridge for row in rows if row.status == "STALE"]
    lines.append("stale: " + (", ".join(stale) if stale else "none"))
    return "\n".join(lines)


def row_to_dict(row):
    return {
        "bridge": row.bridge,
        "pid": row.pid,
        "started": row.started,
        "status": row.status,
        "lag": row.lag,
        "newest": row.newest,
        "note": row.note,
    }


def run(home=None, procs=None, tolerance=TOLERANCE_SECONDS, as_json=False,
        tools_dir=None):
    """Assess and return the exit code (STALE=1, no home=2)."""
    if procs is None:
        procs = read_ps()
    if home is None:
        if tools_dir is None:
            tools_dir = os.path.dirname(os.path.abspath(__file__))
        home = resolve_home(tools_dir, procs)
    if not home:
        sys.stderr.write("bridge_freshness: no home with bridges/ found\n")
        return 2
    rows = assess(home, procs, tolerance=tolerance)
    stale = [row.bridge for row in rows if row.status == "STALE"]
    if as_json:
        print(json.dumps({"home": home, "stale": stale,
                          "rows": [row_to_dict(row) for row in rows]},
                         ensure_ascii=False, indent=2))
    else:
        print("home: " + home)
        print(render(rows))
        if stale:
            print("restart the bridges marked STALE, then re-run this tool")
    return 1 if stale else 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="bridges: a process must be newer than the code it loaded"
    )
    parser.add_argument(
        "--home", help="suite home containing bridges/ (default: auto)"
    )
    parser.add_argument(
        "--tolerance", type=float, default=TOLERANCE_SECONDS,
        help="lag tolerance in seconds (default: %(default)s)",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit machine-readable output"
    )
    args = parser.parse_args(argv)
    return run(home=args.home, tolerance=args.tolerance, as_json=args.json)


if __name__ == "__main__":
    sys.exit(main())

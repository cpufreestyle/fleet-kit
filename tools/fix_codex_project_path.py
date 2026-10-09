#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Repoint the Codex desktop project "fleet-kit" to a new directory.

Run this while Codex is FULLY CLOSED. The desktop app keeps project state in
memory and rewrites .codex-global-state.json on every state change, so edits
made while it is running get reverted. This script patches the persisted state:

  %USERPROFILE%\\.codex\\.codex-global-state.json  sidebar project rootPaths + tab cwd
  %USERPROFILE%\\.codex\\state_5.sqlite             project_roots.path, threads.cwd/sandbox_policy

Every file it touches is backed up first with a timestamp suffix.
"""
import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time

BS = chr(92)
DOC = "\u6587\u6863"
HOME = os.path.expanduser("~")
CODEX_DIR = os.path.join(HOME, ".codex")
STAMP = time.strftime("%Y%m%d-%H%M%S")


def variants(path):
    norm = path.replace("/", BS)
    esc = norm.replace(BS, BS * 2)
    fwd = norm.replace(BS, "/")
    win32 = "win32:/mnt/" + fwd[0].lower() + fwd[2:].lower()
    return (esc, fwd, win32, norm)


def make_pairs(old, new):
    return list(zip(variants(old), variants(new)))


def patch_text(text, pairs):
    hits = 0
    for old_v, new_v in pairs:
        n = text.count(old_v)
        if n:
            text = text.replace(old_v, new_v)
            hits += n
    return text, hits


def ps_prefix():
    exe = shutil.which("pwsh") or shutil.which("pwsh.exe") or "powershell.exe"
    return [exe, "-NoProfile", "-Command"]


def codex_process_count():
    try:
        out = subprocess.run(
            ps_prefix() + ["(Get-Process -Name codex -ErrorAction SilentlyContinue | Measure-Object).Count"],
            capture_output=True, text=True, timeout=60)
        return int((out.stdout or "0").strip() or 0)
    except Exception:
        return 0


def backup(path, tag):
    dst = "%s.bak-%s-%s" % (path, tag, STAMP)
    shutil.copy2(path, dst)
    return dst


def patch_json(codex_dir, pairs, dry_run):
    path = os.path.join(codex_dir, ".codex-global-state.json")
    raw = open(path, "rb").read()
    text = raw.decode("utf-8")
    new_text, hits = patch_text(text, pairs)
    print("json : %s  (%d replacement(s))" % (path, hits))
    if not hits:
        return 0
    parsed = json.loads(new_text)
    if "local-projects" not in parsed:
        raise SystemExit("refusing to write: patched JSON lost local-projects")
    for pid, proj in parsed["local-projects"].items():
        if proj.get("name") == "fleet-kit":
            print("       project %s -> %s" % (pid, proj.get("rootPaths")))
    if dry_run:
        print("       [dry-run] not written")
        return hits
    print("       backup: %s" % backup(path, "migrate"))
    tmp = "%s.tmp-migrate-%s" % (path, STAMP)
    with open(tmp, "wb") as fh:
        fh.write(new_text.encode("utf-8"))
    os.replace(tmp, path)
    bkp = path + ".bak"
    if os.path.exists(bkp):
        btxt = open(bkp, "rb").read().decode("utf-8")
        bnew, bhits = patch_text(btxt, pairs)
        if bhits:
            open(bkp, "wb").write(bnew.encode("utf-8"))
            print("       also patched %s (%d)" % (bkp, bhits))
    return hits


def fix_value(value, pairs):
    if not isinstance(value, str):
        return value, 0
    new_value, hits = patch_text(value, pairs)
    if new_value.startswith(BS * 2 + '?' + BS):
        new_value = new_value[4:]
    if hits and new_value.lstrip().startswith("{"):
        json.loads(new_value)  # validate sandbox_policy JSON
    return new_value, hits


def patch_sqlite(codex_dir, pairs, dry_run):
    path = os.path.join(codex_dir, "state_5.sqlite")
    con = sqlite3.connect(path, timeout=30)
    cur = con.cursor()
    rows = cur.execute(
        "select id, cwd, sandbox_policy from threads "
        "where cwd like '%fleet-kit%' or sandbox_policy like '%fleet-kit%'").fetchall()
    updates = []
    for tid, cwd, policy in rows:
        new_cwd, h1 = fix_value(cwd, pairs)
        new_policy, h2 = fix_value(policy, pairs)
        if h1 or h2:
            updates.append((tid, new_cwd, new_policy, h1, h2))
    roots = []
    for pid, root in cur.execute("select project_id, path from project_roots where path like '%fleet-kit%'"):
        new_root, h = fix_value(root, pairs)
        if h:
            roots.append((pid, new_root, h))
    print("sqlite: %s  (%d thread(s), %d project root(s))" % (path, len(updates), len(roots)))
    for tid, cwd, _, h1, h2 in updates:
        print("       thread %s -> %s" % (tid, cwd))
    for pid, root, _ in roots:
        print("       project_roots %s -> %s" % (pid, root))
    if dry_run:
        print("       [dry-run] not written")
        con.close()
        return len(updates) + len(roots)
    if updates or roots:
        print("       backup: %s" % backup(path, "migrate"))
        for tid, new_cwd, new_policy, _, _ in updates:
            cur.execute("update threads set cwd=?, sandbox_policy=? where id=?", (new_cwd, new_policy, tid))
        for pid, root, _ in roots:
            cur.execute("update project_roots set path=? where project_id=?", (root, pid))
        con.commit()
    left = cur.execute(
        "select count(*) from threads where cwd like '%OneDrive%fleet-kit%' "
        "or sandbox_policy like '%OneDrive%fleet-kit%'").fetchone()[0]
    con.close()
    print("       remaining OneDrive fleet-kit rows: %d" % left)
    return len(updates) + len(roots)


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Repoint the Codex fleet-kit project.")
    ap.add_argument("--old", default=os.path.join(HOME, "OneDrive", DOC, "ChatGPT", "fleet-kit"))
    ap.add_argument("--new", default="D:" + BS + "workspace" + BS + "fleet-kit")
    ap.add_argument("--codex-dir", default=CODEX_DIR)
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    ap.add_argument("--force", action="store_true", help="run even when Codex is still open")
    args = ap.parse_args()

    if not args.dry_run and not args.force:
        count = codex_process_count()
        if count:
            print("Codex is still running (%d process(es)). Quit Codex completely, then run this again." % count)
            return 2
    print("old : %s" % args.old)
    print("new : %s" % args.new)
    pairs = make_pairs(args.old, args.new)
    patch_json(args.codex_dir, pairs, args.dry_run)
    patch_sqlite(args.codex_dir, pairs, args.dry_run)
    if not args.dry_run:
        print("done. Start Codex and check that the fleet-kit project points at %s" % args.new)
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Repoint CC Switch's StepFun provider at the image-cap shim, not past it.

Why this exists, in one sentence: the image-cap shim used to sit in front of CC
Switch, and that arrangement cannot survive a provider switch, because CC Switch
owns ~/.codex/config.toml.

Measured 2026-09-30, on this machine, right after a successful pin:

  * ~/.codex/config.toml read base_url = "http://127.0.0.1:15722/v1" (pinned),
    yet CC Switch's proxy_request_logs still recorded the failing request arriving
    at its own port -- 2026-09-30 03:28:52 and 03:29:15, provider_id
    3a20aad7-bc99-4b10-8a72-d7b7dacd2c16 (StepFun / codex);
  * the shim's own health reported requests=1, i.e. only its self-test. The request
    never touched it;
  * the forwarded target CC Switch logged was
    https://api.stepfun.com/step_plan/v1/chat/completions, straight from its own
    providers row.

A Codex that is already running does not re-read the config, and CC Switch rewrites
that base_url back to 127.0.0.1:15721 every time the operator switches providers,
so a pin can never win reliably. Pinning the file is therefore the wrong lever.

The lever that works is CC Switch's own routing table. Flipping the chain to

    Codex -> 15721 (CC Switch, still owns the config) -> 15722 (shim) -> step_plan

means the cap applies on every path -- whichever process wrote the config, and
whether or not Codex was restarted -- and the shim's upstream is then a real
upstream rather than CC Switch, so the two can never route into a loop. The file
pin becomes a fallback that is off by default rather than the only defence.

This tool performs that flip, once, idempotently:

  * it only ever touches the named provider for the named app_type (default the
    StepFun / codex pair). Every other row, other provider and other app is left
    byte-for-byte alone, so running it on a fleet with no StepFun is a no-op;
  * it rewrites two places, because CC Switch may read either: the
    provider_endpoints.url row it forwards to, and the base_url embedded in
    providers.settings_config (JSON whose "config" value is the TOML the app
    writes into ~/.codex/config.toml);
  * it refuses to repoint a target that is neither the stepfun upstream nor the
    shim itself, so a provider somebody repointed at another host by hand is
    honoured;
  * it takes one timestamped sqlite backup before the first write and never again,
    because a 55MB database that is being rewritten is worth being able to put back;
  * every failure comes back as (False, detail) and nothing raises, so the shim
    can call it from a timer and log the outcome.

CC Switch caches this database in memory: after a write the app has to be restarted
for the new target to be used, which is why the operator restarts it by hand after
setup rather than assuming the next request picks it up.

By default only the named provider for the named app_type moves, and that narrowness
is the point: it is what makes the tool safe for an operator to run by hand, because
a sibling provider somebody repointed elsewhere, and the same provider name under a
different app, are left byte-for-byte alone. --all-stepfun widens the choice to every
provider for the app_type whose current target is StepFun. A fleet can carry more
than one StepFun row -- this machine has codex providers named "StepFun" and
"nv spark", both aimed at api.stepfun.com -- and selecting the second one in the CC
Switch UI would otherwise walk straight past the shim and bring the 400 back. The
allow-list still decides what may move under either spelling, so a provider that
forwards anywhere else survives both.
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import sqlite3
import sys
import time
import urllib.parse

DEFAULT_DB = "~/.cc-switch/cc-switch.db"
DEFAULT_PROVIDER = "StepFun"
DEFAULT_APP_TYPE = "codex"
DEFAULT_SHIM_PORT = 15722

BACKUP_PREFIX = "cc-switch.db.bak-before-fleetkit-endpoint-"


def expand(path):
    """Expanduser/abspath, so a caller may hand us a relative or ~-prefixed path."""
    return os.path.abspath(os.path.expanduser(path or DEFAULT_DB))


def shim_base_url(host="127.0.0.1", port=DEFAULT_SHIM_PORT):
    """The /v1 spelling Codex and CC Switch both expect, not a bare hostport."""
    return "http://%s:%d/v1" % (host, port)


def connect(db_path):
    """Open the database the way a background job must.

    CC Switch holds this file open, so a plain connect fails with "database is
    locked" whenever the two overlap. busy_timeout turns that into a wait, which
    is what a timer wants; the connection is otherwise read/write with the default
    isolation level, so a caller that forgets to commit writes nothing.
    """
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def lookup(conn, provider, app_type):
    """Return (provider id, settings_config) for the named provider, or None.

    providers is keyed by (id, app_type): the same provider can exist for claude
    and codex with different endpoints, which is why app_type is not optional.
    """
    row = conn.execute(
        "select id, settings_config from providers where name=? and app_type=?",
        (provider, app_type)).fetchone()
    if row is None:
        return None
    return row[0], row[1]


def _endpoint_target(conn, provider_id, app_type):
    """The URL CC Switch forwards this provider to, from provider_endpoints."""
    row = conn.execute(
        "select url from provider_endpoints where provider_id=? and app_type=?",
        (provider_id, app_type)).fetchone()
    if row is None:
        return None
    return (row[0] or "").strip()


def _config_target(settings_config):
    """The base_url embedded in the TOML that providers.settings_config carries.

    settings_config is JSON whose "config" value is a TOML document, and the
    base_url of the custom provider is the line CC Switch writes into
    ~/.codex/config.toml, so it has to move with the endpoint row or the app still
    boots Codex at the old address. Substring search rather than a JSON/TOML parse
    because the stored value is not always parseable in the wild, and a partial
    answer beats none.
    """
    # The optional backslashes are not decoration: settings_config is JSON, so a
    # base_url inside the TOML it carries is stored as base_url = \"...\" with
    # escaped quotes. Measured on the live database, a pattern that omits them
    # matches nothing at all and the fallback silently returns None.
    match = re.search(r'base_url\s*=\s*\\?"([^"\\]+)\\"?', settings_config or "")
    return match.group(1).strip() if match else None


def read_target(db_path, provider=DEFAULT_PROVIDER, app_type=DEFAULT_APP_TYPE):
    """Return (current target, its source); (None, detail) when it cannot be read.

    provider_endpoints wins when present because that is the row CC Switch uses to
    forward; the embedded config is the fallback for databases written before the
    endpoints table existed. Nothing raises: a missing or locked database is a
    detail string, not an exception, because every caller is a timer or a setup
    script that has to carry on.
    """
    path = expand(db_path)
    if not os.path.isfile(path):
        return None, "cc-switch db not found: %s" % path
    conn = None
    try:
        conn = connect(path)
        row = lookup(conn, provider, app_type)
        if row is None:
            return None, "no %s provider for app_type=%s" % (provider, app_type)
        provider_id, settings_config = row
        endpoint = _endpoint_target(conn, provider_id, app_type)
        if endpoint:
            return endpoint, "provider_endpoints"
        embedded = _config_target(settings_config)
        if embedded:
            return embedded, "providers.settings_config"
        return None, "no endpoint url and no embedded base_url for %s" % provider
    except Exception as exc:
        return None, "cannot read %s: %s" % (path, exc)
    finally:
        if conn is not None:
            conn.close()


def looks_like_stepfun(target):
    """True only for targets this tool is allowed to repoint.

    The allow-list is deliberately narrow. A target that is already the shim is
    included so a repeat run reports "no change" rather than being skipped
    silently, and the stepfun host because that is the upstream being redirected.
    Anything else -- another local port, another vendor, a typo -- is somebody
    else's decision, and silently rewriting it is worse than not capping at all.
    """
    # A host test, not a substring test: a path that merely contains "stepfun"
    # on somebody else's host is exactly the case the paragraph above calls
    # somebody else's decision, and rewriting it would be worse than not capping.
    low = (target or "").strip().lower()
    if not low:
        return False
    if "://" not in low:
        return False
    parts = urllib.parse.urlsplit(low)
    host = parts.hostname or ""
    if host == "api.stepfun.com":
        return True
    if host in ("127.0.0.1", "localhost"):
        try:
            return parts.port == DEFAULT_SHIM_PORT
        except ValueError:
            return False
    return False


def backup(conn, db_path):
    """One timestamped copy before the first write; later calls do nothing.

    The sqlite backup API keeps the source readable while it copies, so CC Switch
    keeps answering requests during the copy. The glob guard matters because the
    shim calls this on a timer, and a directory filling with 55MB backups is a
    disk problem the operator did not ask for.
    """
    directory = os.path.dirname(db_path) or "."
    pattern = os.path.join(directory, BACKUP_PREFIX + "*")
    if glob.glob(pattern):
        return None
    candidate = os.path.join(
        directory, "%s%s" % (BACKUP_PREFIX, time.strftime("%Y%m%d-%H%M%S")))
    dest = sqlite3.connect(candidate)
    try:
        conn.backup(dest)
    finally:
        dest.close()
    return candidate


def _stepfun_rows(conn, app_type):
    """(provider id, name, current target, its source) for every StepFun row.

    Keyed on the target rather than the name, because the target is what a
    request actually reaches: a row called "nv spark" that forwards to
    api.stepfun.com is StepFun traffic wearing a stale label, and leaving it
    uncapped is the bypass this sweep exists to close. A row whose target is
    anything else is not in the list, so the allow-list stays the only thing
    that ever decides a row may move.
    """
    out = []
    try:
        rows = conn.execute(
            "select id, name, settings_config from providers where app_type=?",
            (app_type,)).fetchall()
    except Exception:
        return out
    for provider_id, name, settings_config in rows:
        target = _endpoint_target(conn, provider_id, app_type)
        source = "provider_endpoints"
        if not target:
            target = _config_target(settings_config)
            source = "providers.settings_config"
        if target and looks_like_stepfun(target):
            out.append((provider_id, name, target, source))
    return out


def _refuse_reason(conn, provider, app_type):
    """Why the named provider is not something this tool may move.

    Returned instead of raising so the wording is identical whether the caller
    asked for one provider or for the whole app_type.
    """
    row = lookup(conn, provider, app_type)
    if row is None:
        return "no %s provider for app_type=%s" % (provider, app_type)
    provider_id, settings_config = row
    target = _endpoint_target(conn, provider_id, app_type)
    if not target:
        target = _config_target(settings_config)
    if not target:
        return "no target to repoint for %s/%s" % (provider, app_type)
    return ("left alone: %s/%s target %s is neither stepfun nor the shim"
            % (provider, app_type, target))


def _repin(conn, path, name, app_type, provider_id, target, source, shim,
           backup_db):
    """Move one provider row to the shim. Returns (changed, detail)."""
    try:
        # The backup must be the database as it stands *before* the first
        # write: the runbook's rollback section restores this file, and a copy
        # taken after the commit would replay the pin instead of undoing it.
        # The glob guard inside backup() keeps this to one file ever, so
        # calling it per row is free -- the second row finds it already there.
        backup_path = backup(conn, path) if backup_db else None

        rows = conn.execute(
            "select id, url from provider_endpoints where provider_id=? and"
            " app_type=?", (provider_id, app_type)).fetchall()
        moved = 0
        if not rows:
            conn.execute(
                "insert into provider_endpoints (provider_id, app_type, url,"
                " added_at) values (?, ?, ?, ?)",
                (provider_id, app_type, shim, int(time.time())))
            moved += 1
        else:
            for row_id, url in rows:
                if looks_like_stepfun(url):
                    cur = conn.execute(
                        "update provider_endpoints set url=? where id=?",
                        (shim, row_id))
                    moved += cur.rowcount or 0
        cur = conn.execute(
            "update providers set settings_config=replace(settings_config, ?, ?)"
            " where id=? and app_type=?",
            (target, shim, provider_id, app_type))
        config_moved = cur.rowcount or 0
        conn.commit()
        detail = ("repointed %s/%s %s: %s -> %s (%d endpoint row%s, %d embedded"
                  " config)"
                  % (name, app_type, source, target, shim, moved,
                     "" if moved == 1 else "s", config_moved))
        if backup_path:
            detail += " [backup %s]" % os.path.basename(backup_path)
        return True, detail
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        return False, "pin failed on %s: %s" % (name, exc)


def pin_once(db_path, shim_base, provider=DEFAULT_PROVIDER,
             app_type=DEFAULT_APP_TYPE, backup_db=True, sweep=False):
    """Repoint the provider at shim_base. Returns (changed, detail); never raises.

    A timer calls this, so it has to be safe to run every few minutes against a
    live application: it re-reads the target immediately before it writes, it only
    writes when that target is one this tool is allowed to move, and it commits
    exactly the update it decided on rather than rewriting rows from a stale read.

    sweep=False keeps the narrow, operator-safe promise: exactly the named
    provider for the named app_type, everything else untouched. sweep=True
    widens that to every provider for the app_type whose target is StepFun --
    see the module docstring for why the shim's timer wants the wide version and
    a one-shot operator run does not.
    """
    shim = (shim_base or "").strip().rstrip("/")
    if not shim:
        return False, "no shim base url given"
    path = expand(db_path)
    if not os.path.isfile(path):
        return False, "cc-switch db not found: %s" % path
    conn = None
    try:
        conn = connect(path)
        if sweep:
            rows = _stepfun_rows(conn, app_type)
            if not rows:
                return False, _refuse_reason(conn, provider, app_type)
        else:
            row = lookup(conn, provider, app_type)
            if row is None:
                return False, ("no %s provider for app_type=%s"
                               % (provider, app_type))
            provider_id, settings_config = row
            target = _endpoint_target(conn, provider_id, app_type)
            source = "provider_endpoints"
            if not target:
                target = _config_target(settings_config)
                source = "providers.settings_config"
            if not target:
                return False, ("no target to repoint for %s/%s"
                               % (provider, app_type))
            if not looks_like_stepfun(target):
                return False, ("left alone: %s/%s target %s is neither stepfun"
                               " nor the shim"
                               % (provider, app_type, target))
            rows = [(provider_id, provider, target, source)]

        done, already = [], []
        for provider_id, name, target, source in rows:
            if target.rstrip("/") == shim:
                already.append(name)
                continue
            changed, detail = _repin(conn, path, name, app_type, provider_id,
                                     target, source, shim, backup_db)
            if changed:
                done.append(detail)
        if not done:
            return False, ("no change: %s already points at %s"
                           % (" and ".join(already) or
                              ("no row for %s/%s" % (provider, app_type)), shim))
        return True, "; ".join(done)
    except Exception as exc:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        return False, "pin failed on %s: %s" % (path, exc)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass



def _dry_run_sweep(args):
    """Report every StepFun-forwarding row for the app_type, writing nothing."""
    try:
        conn = connect(expand(args.db))
    except Exception as exc:
        print("  [dry-run] cc-switch db not found: %s" % exc)
        return 0
    try:
        rows = _stepfun_rows(conn, args.app_type)
    finally:
        conn.close()
    if not rows:
        print("  [dry-run] no %s provider forwards to stepfun" % args.app_type)
        return 0
    for _provider_id, name, target, source in rows:
        if target.rstrip("/") == args.shim.rstrip("/"):
            print("  [dry-run] %s/%s already points at %s (%s)"
                  % (name, args.app_type, target, source))
        else:
            print("  [dry-run] %s/%s forwards to %s (%s); would write it to %s"
                  % (name, args.app_type, target, source, args.shim))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="point CC Switch's StepFun provider at the FleetKit image-cap"
                    " shim instead of straight at StepFun")
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--shim", default=shim_base_url())
    parser.add_argument("--provider", default=DEFAULT_PROVIDER)
    parser.add_argument("--app-type", default=DEFAULT_APP_TYPE)
    parser.add_argument("--dry-run", action="store_true",
                        help="report the current target and what would change")
    parser.add_argument("--all-stepfun", action="store_true",
                        help="every provider for the app_type that forwards to"
                             " StepFun, not just the one named by --provider")
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args(argv)

    if args.dry_run and args.all_stepfun:
        return _dry_run_sweep(args)

    if args.dry_run:
        target, source = read_target(args.db, args.provider, args.app_type)
        if not target:
            print("  [dry-run] %s" % source)
        elif (looks_like_stepfun(target)
              and target.rstrip("/") != args.shim.rstrip("/")):
            print("  [dry-run] %s/%s forwards to %s (%s); would write it to %s"
                  % (args.provider, args.app_type, target, source, args.shim))
        else:
            print("  [dry-run] %s/%s target %s (%s); shim is %s; nothing to do"
                  % (args.provider, args.app_type, target, source, args.shim))
        return 0

    changed, detail = pin_once(args.db, args.shim, args.provider, args.app_type,
                              not args.no_backup, sweep=args.all_stepfun)
    print("  %s" % detail)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

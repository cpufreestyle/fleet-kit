#!/usr/bin/env python3
"""Register the opencodex reverse proxy as a CC Switch provider for Codex.

Why this exists
---------------
opencodex (ocx) is the fleet's reverse proxy: one base URL, every bridge behind
it, 200+ models under bridge-prefixed slugs (workbuddy/..., stepfun/...,
doubao/...). Codex reaches it through the root openai_base_url, but CC Switch
owns ~/.codex/config.toml and rewrites that file every time the operator picks a
provider -- so with no ocx row in CC Switch's own table there is no way to
select the fleet route from the UI, and the provider that *is* selected writes
back a foreign base_url. Measured 2026-10-05: the selected codex provider wrote
[model_providers.custom].base_url = http://127.0.0.1:15722/v1 (the StepFun
image-cap shim), a session resumed with workbuddy/deepseek-v4.1-flash against
it, and StepFun answered 404 "model does not exist" -- the operator saw a dead
model where the truth was a wrong route.

Registering ocx as a provider puts the fleet route in the same list as every
other provider, so switching to it is one click and the config CC Switch then
writes already points at 10100. The row mirrors the claude-side FleetKit
provider: same provider id, one more app_type, which is how this table already
represents a provider that serves several apps.

Two different addresses, and mixing them up is the mistake this replaces:

  * client_url (default http://127.0.0.1:15721/v1) is what Codex talks to.
    It is CC Switch's own proxy port. Keeping the client on it means one choke
    point: CC Switch holds the real vendor keys, can fail a provider over, and
    a switch in its UI takes effect for every leg at once. Measured
    2026-10-07: POST /v1/responses at :15721 forwarded both
    workbuddy/deepseek-v4.1-flash (to ocx) and step-5-preview (to StepFun,
    with the key only CC Switch has).
  * upstream (default http://127.0.0.1:10100/v1) is where CC Switch forwards
    to. It goes into provider_endpoints.url, never into config.toml.

What it does, once, idempotently:

  * inserts or updates the named provider row for the named app_type, and leaves
    every other row -- other app, other provider -- byte-for-byte alone;
  * rewrites two places, because CC Switch may read either: the
    provider_endpoints.url row it forwards to, and the base_url embedded in
    providers.settings_config (JSON whose "config" value is the TOML the app
    writes into ~/.codex/config.toml);
  * reuses the id of an existing row with the same name and app_type, so a
    second run updates rather than duplicates, and falls back to the id the
    claude-side row of the same provider already uses;
  * takes one timestamped sqlite backup before the first write and never again,
    because a 55MB database that is being rewritten is worth being able to put
    back;
  * every failure comes back as (False, detail) and nothing raises, so setup
    can call it and keep going.

CC Switch caches this database in memory: after a write the app has to be
restarted for the new row to show up in the UI, which is why the script says so
in its summary instead of pretending the next request picks it up.

Usage:
  register_cc_switch_provider.py                  register/refresh and set current
  register_cc_switch_provider.py --dry-run        report what would change
  register_cc_switch_provider.py --no-set-current register but keep the current one
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import uuid

DEFAULT_DB = "~/.cc-switch/cc-switch.db"
DEFAULT_APP_TYPE = "codex"
DEFAULT_NAME = "FleetKit"
DEFAULT_UPSTREAM = "http://127.0.0.1:10100/v1"
# What Codex talks to. Default: the proxy itself, no hop in between.
#
# Putting CC Switch's proxy port here (:15721) was tried on 2026-10-07 and does
# not hold: a non-streaming /v1/responses forwards fine, but Codex streams, and
# every streamed request came back 502 "CC Switch local proxy failed" -- which
# tripped its circuit, after which even the good legs answered 503 "所有供应商已熔断".
# So client_url stays equal to upstream by default; pass --client-url
# http://127.0.0.1:15721/v1 to opt into the hop if that ever gets fixed.
DEFAULT_CLIENT_URL = DEFAULT_UPSTREAM
DEFAULT_CATALOG = "~/.codex/opencodex-catalog.json"
DEFAULT_MODEL = "workbuddy/deepseek-v4-flash"
BACKUP_PREFIX = "bak-before-fleetkit-provider-"

# The app CC Switch's own rows notify on turn end. Only added when the app is
# actually installed, so a machine without it does not inherit a dead path.
DEFAULT_NOTIFY = ("/Users/a1-6/.codex/computer-use/Codex Computer Use.app"
                  "/Contents/SharedSupport/SkyComputerUseClient.app"
                  "/Contents/MacOS/SkyComputerUseClient")


def expand(path: str) -> str:
    """Expanduser/abspath, so a caller may hand us a relative or ~-prefixed path."""
    return os.path.abspath(os.path.expanduser(path or DEFAULT_DB))


def connect(db_path: str) -> sqlite3.Connection:
    """Open the database the way a background job must.

    CC Switch holds this file open, so a plain connect fails with "database is
    locked" whenever the two overlap. busy_timeout turns that into a wait.
    """
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def config_toml(client_url: str, catalog: str, model: str, notify: bool) -> str:
    """The TOML CC Switch writes into ~/.codex/config.toml for this provider.

    Every address in here is CC Switch's own proxy port, not the reverse proxy
    behind it: the client talks to CC Switch, CC Switch forwards. Two provider
    tables on purpose. model_provider = "custom" is what every other row in
    this database uses, and the ocx table is the one the fleet's own tooling
    writes; both name the same address, so a config produced by either side --
    or half-overwritten by one of them -- still lands on CC Switch instead of
    on a bridge that answers 404 for a foreign slug.
    """
    lines = [
        'model_provider = "custom"',
        'openai_base_url = "%s"' % client_url,
        'experimental_realtime_ws_base_url = "%s"' % client_url,
        'model = "%s"' % model,
        'model_reasoning_effort = "high"',
        "disable_response_storage = true",
        'model_catalog_json = "%s"' % catalog,
    ]
    if notify and os.path.exists(DEFAULT_NOTIFY):
        lines.append('notify = [ "%s", "turn-ended" ]' % DEFAULT_NOTIFY)
    lines += [
        'sandbox_mode = "danger-full-access"',
        "",
        "[model_providers.custom]",
        'name = "opencodex"',
        'base_url = "%s"' % client_url,
        'wire_api = "responses"',
        # ocx authenticates the bridges itself; a key here would be an unknown
        # credential to it.
        "requires_openai_auth = false",
        "",
        "[model_providers.opencodex]",
        'name = "FleetKit Gateway"',
        'base_url = "%s"' % client_url,
        'wire_api = "responses"',
        "requires_openai_auth = false",
        "",
        '[projects."/Users/a1-6"]',
        'trust_level = "trusted"',
        "",
        "[desktop]",
        'followUpQueueMode = "queue"',
        "",
    ]
    return "\n".join(lines)


def resolve_id(conn: sqlite3.Connection, name: str, app_type: str,
               wanted: str) -> str:
    """Pick the provider id to write under, reusing one wherever possible."""
    if wanted:
        return wanted
    row = conn.execute("select id from providers where name = ? and app_type = ?"
                       " order by created_at limit 1", (name, app_type)).fetchone()
    if row and row[0]:
        return row[0]
    # Same provider, another app: keep one id across the apps, the way the
    # claude and claude-desktop rows already do.
    row = conn.execute("select id from providers where name = ? and app_type != ?"
                       " order by created_at limit 1", (name, app_type)).fetchone()
    if row and row[0]:
        return row[0]
    return str(uuid.uuid4())


def backup_once(db_path: str, dry_run: bool) -> str:
    """One timestamped copy, because a 55MB live database deserves a way back."""
    target = "%s.%s%s" % (db_path, BACKUP_PREFIX,
                          time.strftime("%Y%m%d-%H%M%S"))
    if dry_run:
        return "%s (dry-run, not written)" % target
    with open(db_path, "rb") as src, open(target, "wb") as dst:
        while True:
            chunk = src.read(1 << 20)
            if not chunk:
                break
            dst.write(chunk)
    return target


def register(db_path: str, name: str, app_type: str, upstream: str,
             catalog: str, model: str, provider_id: str = "",
             client_url: str = DEFAULT_CLIENT_URL, set_current: bool = True,
             notify: bool = True, dry_run: bool = False,
             backup: bool = True) -> tuple[bool, str]:
    """Insert or refresh the provider row. Returns (changed, detail)."""
    # sqlite3.connect creates the file it is handed, so a typo in --db would
    # otherwise register into a fresh empty database and report success.
    if not os.path.exists(db_path):
        return False, "database not found: %s" % db_path
    try:
        conn = connect(db_path)
    except sqlite3.Error as exc:
        return False, "cannot open %s: %s" % (db_path, exc)
    try:
        if not conn.execute("select 1 from sqlite_master where type = 'table'"
                            " and name = 'providers'").fetchone():
            return False, "no providers table in %s" % db_path
        pid = resolve_id(conn, name, app_type, provider_id)
        exists = conn.execute(
            "select 1 from providers where id = ? and app_type = ?",
            (pid, app_type)).fetchone() is not None
        toml = config_toml(client_url, expand_catalog(catalog), model, notify)
        settings = json.dumps({"auth": {"OPENAI_API_KEY": "dummy"},
                               "config": toml}, ensure_ascii=False)
        meta = json.dumps({"commonConfigEnabled": True,
                           "endpointAutoSelect": True,
                           "apiFormat": "openai_responses"}, ensure_ascii=False)
        notes = ("client -> %s (CC Switch) -> %s -> fleet bridges"
                 % (client_url, upstream))
        if dry_run:
            return (not exists or True,
                    "would %s %s/%s (id %s): client %s, upstream %s"
                    % ("update" if exists else "insert", name, app_type, pid,
                       client_url, upstream))
        saved = backup_once(db_path, dry_run) if backup else ""
        now = int(time.time() * 1000)
        if exists:
            # No updated_at on this schema: providers carries created_at only,
            # so a refresh rewrites the row without touching its timestamps
            # rather than failing with "no such column".
            conn.execute("update providers set name = ?, settings_config = ?,"
                         " notes = ?, meta = ?, provider_type = 'custom'"
                         " where id = ? and app_type = ?",
                         (name, settings, notes, meta, pid, app_type))
        else:
            conn.execute("insert into providers (id, app_type, name,"
                         " settings_config, website_url, category, created_at,"
                         " sort_index, notes, icon, icon_color, meta,"
                         " is_current, in_failover_queue, cost_multiplier,"
                         " limit_daily_usd, limit_monthly_usd, provider_type)"
                         " values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (pid, app_type, name, settings, None, None, now, 0,
                          notes, None, None, meta, 0, 0, "1.0", None, None,
                          "custom"))
        # One endpoint row per (provider, app), replaced rather than appended:
        # the table has no unique constraint and a second row would leave CC
        # Switch forwarding to whichever it reads first.
        conn.execute("delete from provider_endpoints where provider_id = ?"
                     " and app_type = ?", (pid, app_type))
        conn.execute("insert into provider_endpoints (provider_id, app_type,"
                     " url, added_at) values (?,?,?,?)",
                     (pid, app_type, upstream, now))
        if set_current:
            conn.execute("update providers set is_current = 0"
                         " where app_type = ? and id != ?", (app_type, pid))
            conn.execute("update providers set is_current = 1"
                         " where app_type = ? and id = ?", (app_type, pid))
        conn.commit()
        detail = ("%s %s/%s (id %s): client -> %s, upstream %s; endpoint row"
                  " written%s%s"
                  % ("updated" if exists else "inserted", name, app_type, pid,
                     client_url, upstream,
                     "; set as the current provider" if set_current else "",
                     "; backup %s" % os.path.basename(saved) if saved else ""))
        return True, detail
    except sqlite3.Error as exc:
        return False, "database error: %s" % exc
    finally:
        conn.close()


def expand_catalog(catalog: str) -> str:
    """Catalog paths are absolute in the TOML; ~ would not expand there."""
    return os.path.abspath(os.path.expanduser(catalog or DEFAULT_CATALOG))


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="register the opencodex reverse proxy in CC Switch")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--app-type", default=DEFAULT_APP_TYPE)
    p.add_argument("--name", default=DEFAULT_NAME)
    p.add_argument("--provider-id", default="",
                   help="reuse this id instead of resolving one (default:"
                        " the existing row's, else the same provider's other"
                        " app, else a new uuid)")
    p.add_argument("--base-url", dest="upstream", default=DEFAULT_UPSTREAM,
                   help="where CC Switch forwards to (default %s)"
                        % DEFAULT_UPSTREAM)
    p.add_argument("--client-url", default=DEFAULT_CLIENT_URL,
                   help="address written into config.toml, i.e. CC Switch's"
                        " own proxy port (default %s)" % DEFAULT_CLIENT_URL)
    p.add_argument("--catalog", default=DEFAULT_CATALOG)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--no-set-current", action="store_true",
                   help="register but leave the current provider alone")
    p.add_argument("--no-notify", action="store_true",
                   help="omit the notify entry from the written config")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-backup", action="store_true")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    changed, detail = register(
        expand(args.db), args.name, args.app_type, args.upstream,
        args.catalog, args.model, provider_id=args.provider_id,
        client_url=args.client_url,
        set_current=not args.no_set_current, notify=not args.no_notify,
        dry_run=args.dry_run, backup=not args.no_backup)
    print("%s %s" % ("[dry-run] would change:" if args.dry_run
                     else ("ok:" if changed else "no change:"), detail))
    if not args.dry_run and changed:
        print("CC Switch caches this database in memory: restart the app to"
              " see the provider in its list.")
    return 0 if changed else 1


if __name__ == "__main__":
    sys.exit(main())

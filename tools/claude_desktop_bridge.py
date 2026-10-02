#!/usr/bin/env python3
"""Point the Claude Code desktop app's 3P profile at the FleetKit gateway.

The desktop app keeps its inference profiles in a configLibrary under its
3P user-data directory, and _meta.json names the one entry that is applied.
This tool writes a FleetKit entry there and applies it, leaving every other
entry (cc-switch and friends) untouched so switching back is one command.

Why the entry carries no inferenceModels list. The app discovers models by
polling GET {base}/v1/models?limit=1000 at launch, and keeps a row when
either its id looks Anthropic-shaped or the row carries
anthropic_family_tier. FleetKit's gateway tags every catalog row with that
tier, so discovery lists the whole pool. Writing an explicit inferenceModels
list instead makes discovery unnecessary and the app then skips it entirely,
which is how a 121-model gateway ends up showing four models.

The layout below is read out of app.asar 1.32352.1 (function gf(), plus
SPe=/^[a-f0-9-]{36}$/ guarding entry ids):

  CLAUDE_USER_DATA_DIR   wins whenever it is set
  win32                  %LOCALAPPDATA%/Claude-3p
  everything else        <app userData>-3p
      darwin             ~/Library/Application Support/Claude-3p
      linux              ~/.config/Claude-3p

Usage:
  claude_desktop_bridge.py             apply the FleetKit entry
  claude_desktop_bridge.py status      show what is applied and whether it answers
  claude_desktop_bridge.py off         step back to whatever was applied before
  claude_desktop_bridge.py remove      delete the entry and unapply it
  claude_desktop_bridge.py --dry-run apply   print the plan, write nothing

The app reads this config at launch, so a running Claude.app has to be
restarted for apply to take effect. Nothing here closes it.
"""
import argparse
import json
import os
import re
import shutil
import sys
import time
import urllib.request

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8801
DEFAULT_TOKEN = "sk-fleetkit-local"
ENTRY_NAME = "FleetKit"
CONFIG_LIBRARY = "configLibrary"
META_FILE = "_meta.json"
ENTRY_ID_RE = re.compile(r"^[a-f0-9-]{36}$")

# fields the app reads off an applied entry; every one is a flat key from the
# app's own settings schema, spelled the way the app spells it
ENTRY_FIELDS = (
    "inferenceProvider",
    "inferenceGatewayBaseUrl",
    "inferenceGatewayAuthScheme",
    "inferenceGatewayApiKey",
    "coworkEgressAllowedHosts",
    "disableDeploymentModeChooser",
)


# ------------------------------------------------------------------ locations
def platform_name():
    """sys.platform on its own, so the location rules are testable per OS.

    Patching sys.platform directly would rewrite it for the whole test
    process, pytest included, which is a fast way to make the suite lie.
    """
    return sys.platform


def user_data_dir(home=None):
    """Where the app keeps its 3P profile, on this machine.

    home forces a directory outright: that is how the tests run the real
    code against a scratch tree instead of the live profile.
    """
    if home:
        return os.path.abspath(os.path.expanduser(home))
    forced = os.environ.get("CLAUDE_USER_DATA_DIR")
    if forced:
        return os.path.abspath(os.path.expanduser(forced))
    if platform_name().startswith("win"):
        local = os.environ.get("LOCALAPPDATA")
        if local:
            return os.path.join(local, "Claude-3p")
        return os.path.join(os.path.expanduser("~"), "AppData", "Local", "Claude-3p")
    if platform_name() == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Application Support",
                            "Claude-3p")
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config")
    return os.path.join(base, "Claude-3p")


def config_library(home=None):
    return os.path.join(user_data_dir(home), CONFIG_LIBRARY)


def entry_path(entry_id, home=None):
    return os.path.join(config_library(home), entry_id + ".json")


def meta_path(home=None):
    return os.path.join(config_library(home), META_FILE)


def default_entry_id(port):
    """A stable id for a port, in the shape cc-switch already uses.

    cc-switch encodes 15721 as 00000000-0000-4000-8000-000000157210, so the
    gateway's port stays legible in the id and two FleetKit installs on
    different ports cannot collide.
    """
    return "00000000-0000-4000-8000-%012d" % (int(port) * 10)


# ----------------------------------------------------------------------- json
def read_json(path):
    """A parsed JSON file, or None when it is missing or unreadable."""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def write_json(path, data):
    """Write JSON the way the app does: two spaces, trailing newline."""
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def backup(path, stamp):
    """A .bak-<stamp> copy of a file that is about to change."""
    if not os.path.exists(path):
        return None
    target = "%s.bak-%s" % (path, stamp)
    shutil.copy2(path, target)
    return target


# --------------------------------------------------------------------- entry
def fleetkit_entry(base_url, token, inherit=None):
    """The FleetKit entry, keeping the non-gateway fields it inherits.

    coworkEgressAllowedHosts and disableDeploymentModeChooser are about the
    sandbox and the login screen, not about which gateway answers, so they
    are carried over from the entry being replaced when it has them. Without
    that, bridging a profile would quietly hand the Claude.ai sign-in back
    to the user.
    """
    entry = {
        "inferenceProvider": "gateway",
        "inferenceGatewayBaseUrl": base_url,
        "inferenceGatewayAuthScheme": "bearer",
        "inferenceGatewayApiKey": token,
    }
    for field in ("coworkEgressAllowedHosts", "disableDeploymentModeChooser"):
        value = (inherit or {}).get(field)
        if value is not None:
            entry[field] = value
    return entry


# ------------------------------------------------------------------ meta file
def applied_id(home=None):
    """The entry id _meta.json applies, or "" when nothing is applied."""
    meta = read_json(meta_path(home)) or {}
    value = meta.get("appliedId")
    return value if isinstance(value, str) else ""


def listed_entries(home=None):
    """[{id, name}] for every entry in the library, library order kept."""
    meta = read_json(meta_path(home)) or {}
    listed = meta.get("entries")
    if not isinstance(listed, list):
        return []
    out = []
    for item in listed:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            out.append({"id": item["id"], "name": item.get("name") or item["id"]})
    return out


def upsert_meta(meta, entry_id, name):
    """Register the entry first and apply it, leaving the others alone."""
    listed = [e for e in (meta.get("entries") or [])
              if not (isinstance(e, dict) and e.get("id") == entry_id)]
    listed.insert(0, {"id": entry_id, "name": name})
    meta["entries"] = listed
    meta["appliedId"] = entry_id
    return meta


# ------------------------------------------------------------------ discovery
def probe_gateway(base_url, timeout=8.0):
    """What GET {base}/v1/models says, in the shape the app asks for it.

    The app sends Authorization: Bearer <key> plus anthropic-version, and
    refuses to follow a redirect, so the probe uses exactly that. Returns
    (http status, listed models) and never raises: a gateway that is down
    is a fact to report, not a crash.
    """
    url = base_url.rstrip("/") + "/v1/models?limit=1000"
    request = urllib.request.Request(
        url, headers={"Authorization": "Bearer " + DEFAULT_TOKEN,
                      "anthropic-version": "2023-06-01"},
        method="GET")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:
            detail = ""
        return exc.code, detail
    except Exception as exc:
        return None, "%s: %s" % (type(exc).__name__, exc)
    rows = body.get("data") if isinstance(body, dict) else None
    return 200, [r.get("id") for r in rows or [] if isinstance(r, dict)]


# -------------------------------------------------------------------- actions
def cmd_apply(args):
    home = user_data_dir(args.home)
    library = config_library(home)
    entry_id = args.id or default_entry_id(args.port)
    if not ENTRY_ID_RE.match(entry_id):
        print("refusing %r: an entry id must match %s"
              % (entry_id, ENTRY_ID_RE.pattern), file=sys.stderr)
        return 2
    base_url = "http://%s:%d" % (args.host, args.port)
    stamp = time.strftime("%Y%m%d-%H%M%S")

    previous = read_json(entry_path(applied_id(home), home)) or {}
    entry = fleetkit_entry(base_url, args.token, inherit=previous)
    meta = read_json(meta_path(home)) or {}
    meta = upsert_meta(meta, entry_id, args.name)

    plan = [
        "user data dir : %s" % home,
        "config library: %s" % library,
        "entry         : %s (%s)" % (entry_id, args.name),
        "base url      : %s" % base_url,
        "health probe  : GET %s/v1/models?limit=1000" % base_url,
    ]
    for line in plan:
        print(line)
    print(json.dumps(entry, indent=2, ensure_ascii=False))
    if args.dry_run:
        print("dry run: nothing written")
        return 0

    made = []
    for path in (meta_path(home), entry_path(entry_id, home)):
        copy = backup(path, stamp)
        if copy:
            made.append(copy)
    write_json(entry_path(entry_id, home), entry)
    write_json(meta_path(home), meta)
    for copy in made:
        print("backup        : %s" % copy)
    print("applied       : %s" % entry_id)
    print("restart Claude.app for the picker to pick this up")
    return 0


def cmd_off(args):
    """Step back to the entry that was applied before FleetKit."""
    home = user_data_dir(args.home)
    entry_id = args.id or default_entry_id(args.port)
    meta = read_json(meta_path(home))
    if meta is None:
        print("no config library at %s" % config_library(home), file=sys.stderr)
        return 1
    if meta.get("appliedId") != entry_id:
        print("FleetKit is not the applied entry (%s); nothing to step back from"
              % (meta.get("appliedId") or "(none)"), file=sys.stderr)
        return 1
    others = [e for e in (meta.get("entries") or [])
              if isinstance(e, dict) and e.get("id") != entry_id]
    if not others:
        print("nothing to step back to: FleetKit is the only entry",
              file=sys.stderr)
        return 1
    stamp = time.strftime("%Y%m%d-%H%M%S")
    copy = backup(meta_path(home), stamp)
    meta["appliedId"] = others[0]["id"]
    write_json(meta_path(home), meta)
    if copy:
        print("backup        : %s" % copy)
    print("applied       : %s (%s)" % (others[0]["id"], others[0].get("name")))
    print("restart Claude.app for the picker to pick this up")
    return 0


def cmd_remove(args):
    """Unapply and delete the FleetKit entry, keeping every other entry."""
    home = user_data_dir(args.home)
    entry_id = args.id or default_entry_id(args.port)
    library = config_library(home)
    path = entry_path(entry_id, home)
    if not os.path.exists(path):
        print("no entry %s in %s" % (entry_id, library))
        return 0
    stamp = time.strftime("%Y%m%d-%H%M%S")
    meta = read_json(meta_path(home)) or {}
    kept = [e for e in (meta.get("entries") or [])
            if not (isinstance(e, dict) and e.get("id") == entry_id)]
    copy = backup(meta_path(home), stamp)
    if kept:
        meta["entries"] = kept
        if meta.get("appliedId") == entry_id:
            meta["appliedId"] = kept[0]["id"]
        write_json(meta_path(home), meta)
    else:
        os.remove(meta_path(home))
    os.remove(path)
    if copy:
        print("backup        : %s" % copy)
    print("removed       : %s" % path)
    print("applied now   : %s" % (meta.get("appliedId") if kept else "(none)"))
    return 0


def cmd_status(args):
    """What is applied, and whether that gateway actually answers."""
    home = user_data_dir(args.home)
    library = config_library(home)
    if not os.path.isdir(library):
        print("no config library at %s" % library)
        return 1
    print("user data dir : %s" % home)
    print("config library: %s" % library)
    for item in listed_entries(home):
        mark = "*" if item["id"] == applied_id(home) else " "
        body = read_json(entry_path(item["id"], home)) or {}
        print("  %s %s  %s  %s" % (mark, item["id"], item["name"],
                                   body.get("inferenceGatewayBaseUrl", "")))
    body = read_json(entry_path(applied_id(home), home)) or {}
    base_url = body.get("inferenceGatewayBaseUrl")
    if not base_url:
        print("applied entry carries no gateway base url")
        return 0
    status, answer = probe_gateway(base_url)
    if status == 200:
        print("gateway       : %s -> %d models" % (base_url, len(answer)))
        for slug in answer[:5]:
            print("    %s" % slug)
    else:
        print("gateway       : %s -> %s %s" % (base_url, status, answer))
    return 0


# ----------------------------------------------------------------------- main
def build_parser():
    ap = argparse.ArgumentParser(
        description="Point the Claude Code desktop app's 3P profile at FleetKit.")
    ap.add_argument("command", nargs="?", default="apply",
                    choices=["apply", "off", "remove", "status"])
    ap.add_argument("--home", default=None,
                    help="3P user-data directory to edit instead of the real one")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--token", default=DEFAULT_TOKEN,
                    help="value sent as the gateway bearer token")
    ap.add_argument("--name", default=ENTRY_NAME)
    ap.add_argument("--id", default=None,
                    help="entry id; defaults to a stable one built from --port")
    ap.add_argument("--dry-run", action="store_true")
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    handlers = {"apply": cmd_apply, "off": cmd_off,
                "remove": cmd_remove, "status": cmd_status}
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())

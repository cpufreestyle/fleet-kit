#!/usr/bin/env bash
# ocx-catalog-guard.sh -- keep the fleet bridge models in the Codex model catalog.
#
# Why this exists
# ---------------
# opencodex (ocx) appends the fleet bridge models into whichever catalog model_catalog_json
# names in ~/.codex/config.toml. When a third-party provider switcher (CC Switch) owns that
# file, or the catalog is otherwise regenerated, the bridge models disappear from the Codex
# model picker: restart the app and the reverse-proxied models can no longer be selected.
#
# ocx ensure does NOT heal this. It refuses to inject while an external model_provider owns
# config.toml, so a stripped catalog stays stripped. Only ocx sync refreshes the catalog and
# the models cache. Verified live 2026-09-26: simulated the wipe, ran ocx ensure, the bridge
# model count stayed at 0.
#
# This guard counts the slash-prefixed bridge models in the live catalog and re-runs
# ocx sync when they drop below MIN_MODELS. Cheap enough for a 5 minute launchd timer.
#
# Usage:
#   ocx-catalog-guard.sh run                 check and heal (default)
#   ocx-catalog-guard.sh install-timer       launchd timer, default every 300s
#   ocx-catalog-guard.sh uninstall-timer
#   ocx-catalog-guard.sh status
#
# Options:
#   --codex-home DIR   Codex home (default: ~/.codex, honours $CODEX_HOME)
#   --min-models N     heal below this many bridge models (default: 60)
#   --interval SEC     timer interval in seconds (default: 300)
#   --log FILE         log file (default: ~/Library/Logs/ocx-catalog-guard.log)
#   --dry-run          report what would happen, change nothing
#   -h | --help
set -euo pipefail

MIN_MODELS=60
INTERVAL=300
CODEX_HOME="${CODEX_HOME:-${HOME}/.codex}"
LOG_FILE="${OCX_GUARD_LOG:-${HOME}/Library/Logs/ocx-catalog-guard.log}"
LAUNCH_DIR="${FLEET_LAUNCH_DIR:-${HOME}/Library/LaunchAgents}"
LABEL_PREFIX="${FLEET_LABEL_PREFIX:-com.local}"
DRY_RUN=0
ACTION=run

usage() {
  cat <<USAGE
ocx-catalog-guard.sh -- keep the fleet bridge models in the Codex model catalog

  run                 check the catalog and heal it (default)
  install-timer       launchd timer, every 300s by default
  uninstall-timer     remove the launchd timer
  status              current bridge-model count, timer state, last log lines

Options:
  --codex-home DIR    Codex home (default ~/.codex)
  --min-models N      heal below this many bridge models (default 60)
  --interval SEC      timer interval (default 300)
  --log FILE          log file
  --dry-run           report only
USAGE
}

log() {
  echo "$(date "+%Y-%m-%d %H:%M:%S") $*" >>"$LOG_FILE" 2>/dev/null || true
}

# Count the slash-prefixed bridge models in the catalog that model_catalog_json names.
# Prints a number, or a non-numeric reason when the count cannot be taken.
catalog_bridge_count() {
  python3 - "$CODEX_HOME" <<PY
import json, os, sys

home = sys.argv[1]
name = None
try:
    with open(os.path.join(home, "config.toml"), encoding="utf-8") as fh:
        for line in fh:
            s = line.strip()
            if s.startswith("model_catalog_json"):
                name = s.split("=", 1)[1].strip().strip(chr(34))
                break
except OSError:
    pass
if not name:
    print("no-catalog-declared")
    sys.exit(0)

path = name if os.path.isabs(name) else os.path.join(home, name)
try:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
except Exception:
    print("catalog-unreadable")
    sys.exit(0)

models = data.get("models") or []


def slug(m):
    if isinstance(m, dict):
        return m.get("slug") or m.get("id") or m.get("model") or ""
    return ""


print(len([s for s in (slug(m) for m in models) if "/" in s]))
PY
}

cmd_run() {
  mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true

  if ! command -v ocx >/dev/null 2>&1; then
    log "skip: ocx not on PATH"
    return 0
  fi

  count="$(catalog_bridge_count || echo unknown)"
  if ! printf "%s" "$count" | grep -q "^[0-9][0-9]*$"; then
    log "skip: cannot count bridge models ($count)"
    return 0
  fi

  if [ "$count" -ge "$MIN_MODELS" ]; then
    if [ "${OCX_GUARD_VERBOSE:-0}" = "1" ]; then
      log "ok: $count bridge models in catalog"
    fi
    return 0
  fi

  log "heal: only $count bridge models, below min $MIN_MODELS, running ocx sync"
  if [ "$DRY_RUN" = "1" ]; then
    log "[dry-run] ocx sync"
    return 0
  fi
  if ocx sync >>"$LOG_FILE" 2>&1; then
    after="$(catalog_bridge_count || echo unknown)"
    log "heal done: $count bridge models, now $after"
  else
    log "heal FAILED: ocx sync exited non-zero"
    return 1
  fi
}

cmd_install_timer() {
  label="${LABEL_PREFIX}.ocx-catalog-guard"
  plist="${LAUNCH_DIR}/${label}.plist"
  script_path="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
  mkdir -p "$LAUNCH_DIR"
  cat >"$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>${script_path}</string>
    <string>run</string>
  </array>
  <key>StartInterval</key>
  <integer>${INTERVAL}</integer>
  <key>RunAtLoad</key>
  <true/>
  <key>StandardOutPath</key>
  <string>${LOG_FILE}</string>
  <key>StandardErrorPath</key>
  <string>${LOG_FILE}</string>
</dict>
</plist>
PLIST
  launchctl unload "$plist" 2>/dev/null || true
  launchctl load "$plist"
  echo "installed ${label} (every ${INTERVAL}s), plist: ${plist}"
}

cmd_uninstall_timer() {
  label="${LABEL_PREFIX}.ocx-catalog-guard"
  plist="${LAUNCH_DIR}/${label}.plist"
  launchctl unload "$plist" 2>/dev/null || true
  unlink "$plist" 2>/dev/null || true
  echo "removed ${label}"
}

cmd_status() {
  label="${LABEL_PREFIX}.ocx-catalog-guard"
  count="$(catalog_bridge_count || echo unknown)"
  echo "codex home  : ${CODEX_HOME}"
  echo "bridge models in catalog: ${count} (heal below ${MIN_MODELS})"
  echo "log         : ${LOG_FILE}"
  if [ -f "${LAUNCH_DIR}/${label}.plist" ]; then
    echo "timer       : installed (every ${INTERVAL}s)"
  else
    echo "timer       : not installed"
  fi
  if [ -f "$LOG_FILE" ]; then
    echo "last log lines:"
    tail -5 "$LOG_FILE"
  fi
  return 0
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    run) ACTION="run" ;;
    install-timer) ACTION="install-timer" ;;
    uninstall-timer) ACTION="uninstall-timer" ;;
    status) ACTION="status" ;;
    --codex-home) shift; CODEX_HOME="${1:?--codex-home needs a value}" ;;
    --codex-home=*) CODEX_HOME="${1#*=}" ;;
    --min-models) shift; MIN_MODELS="${1:?--min-models needs a value}" ;;
    --min-models=*) MIN_MODELS="${1#*=}" ;;
    --interval) shift; INTERVAL="${1:?--interval needs a value}" ;;
    --interval=*) INTERVAL="${1#*=}" ;;
    --log) shift; LOG_FILE="${1:?--log needs a value}" ;;
    --log=*) LOG_FILE="${1#*=}" ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

case "$ACTION" in
  run) cmd_run ;;
  install-timer) cmd_install_timer ;;
  uninstall-timer) cmd_uninstall_timer ;;
  status) cmd_status ;;
esac

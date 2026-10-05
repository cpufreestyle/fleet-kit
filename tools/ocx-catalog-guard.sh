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
# The same switch also takes the fleet route with it: it drops openai_base_url and the
# [model_providers.opencodex] table and sets model_provider at its own provider, so a fleet
# model picked from the still-populated picker is sent to a foreign upstream and answered
# 404 "model does not exist" (measured 2026-09-30, lingxi/lingxi-deepseek-flash against
# api.stepfun.com/step_plan/v1/responses). route_pin() therefore runs first and puts the
# route back with tools/pin_fleet_route.py, which touches only FleetKit's own keys so the
# switcher's provider -- the one every already-open session resolves -- survives.
#
# A count that cannot be taken now heals too. It used to end the run with "skip", and a
# config stripped of its declaration reported skip every 300s forever: measured 2026-09-30
# 11:22 and 11:27, "skip: cannot count bridge models (no-catalog-declared)" with 123 bridge
# models sitting in a catalog file nobody declared.
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

# Resolve without dirname: a Task Scheduler action inherits the bare machine
# PATH, which has no Git for Windows coreutils, so $(dirname) would abort the
# script under set -e before it can log anything. The cmd wrapper passes a
# Windows-style $0 with backslashes, which ${0%/*} cannot strip -- normalise
# the separators first or SCRIPT_DIR degrades to the cwd and every helper
# lookup (platform.sh, pin_fleet_route.py) silently misses.
GUARD_ZERO="${0//\\//}"
case "$GUARD_ZERO" in
  */*) SCRIPT_DIR="$(cd "${GUARD_ZERO%/*}" && pwd)" ;;
  *) SCRIPT_DIR="$(pwd)" ;;
esac

MIN_MODELS=60
INTERVAL=300
CODEX_HOME="${CODEX_HOME:-${HOME}/.codex}"
LOG_FILE="${OCX_GUARD_LOG:-${HOME}/Library/Logs/ocx-catalog-guard.log}"

# Platform abstraction: launchd / Task Scheduler / Linux supervisor.
if [ -f "${SCRIPT_DIR}/platform.sh" ]; then
  # shellcheck source=platform.sh
  . "${SCRIPT_DIR}/platform.sh"
fi

# The inline JSON probe and the route pin need a real interpreter. A bare
# "python3" on Windows usually resolves to the Store/WSL stub, which either
# launches WSL (where the C:\\... argv paths do not exist) or prints a proxy
# warning -- so Windows prefers "python", POSIX prefers "python3".
FLEET_PY_BIN="${FLEET_PYTHON:-}"
if [ -z "$FLEET_PY_BIN" ] || [ ! -x "$FLEET_PY_BIN" ]; then
  if fleet_is_windows 2>/dev/null; then
    FLEET_PY_BIN="$(command -v python 2>/dev/null || command -v python3 2>/dev/null || printf '')"
  else
    FLEET_PY_BIN="$(command -v python3 2>/dev/null || command -v python 2>/dev/null || printf '')"
  fi
fi
if command -v fleet_service_dir >/dev/null 2>&1; then
  LAUNCH_DIR="$(fleet_service_dir)"
fi
LAUNCH_DIR="${FLEET_LAUNCH_DIR:-${LAUNCH_DIR:-${HOME}/Library/LaunchAgents}}"
FLEET_SERVICE_DIR="${FLEET_SERVICE_DIR:-${LAUNCH_DIR}}"
export FLEET_SERVICE_DIR
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

# Count the slash-prefixed bridge models in the catalog that model_catalog_json
# names, plus how many slash rows catalog_filter.py hid on purpose. Prints
# "<count> <hidden>", or "<reason> 0" when the count cannot be taken.
#
# The second number is what keeps this guard from fighting the filter: the
# filter hides rows for bridges verified not REAL, and healing that would
# re-add every broken row for the filter to hide again 300s later.
catalog_bridge_count() {
  # No interpreter (or only the WSL stub) reads as "cannot count" and
  # the caller degrades to its no-heal path.
  [ -n "$FLEET_PY_BIN" ] || { echo "no python interpreter found 0"; return 0; }
  "$FLEET_PY_BIN" - "$CODEX_HOME" <<PY
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
    print("no-catalog-declared 0")
    sys.exit(0)

path = name if os.path.isabs(name) else os.path.join(home, name)
hidden = 0
try:
    with open(os.path.join(os.path.dirname(path),
                           ".catalog-filter-hidden.json"),
              encoding="utf-8") as fh:
        hidden = int(json.load(fh).get("slash_rows_hidden") or 0)
except Exception:
    hidden = 0
try:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
except Exception:
    print("catalog-unreadable 0")
    sys.exit(0)

models = data.get("models") or []


def slug(m):
    if isinstance(m, dict):
        return m.get("slug") or m.get("id") or m.get("model") or ""
    return ""


print("%d %d" % (len([s for s in (slug(m) for m in models) if "/" in s]),
                 hidden))
PY
}

# Put the fleet route back before counting anything. The pin is idempotent, so this
# is a no-op on a healthy config and only spends a write when the switcher struck.
# FLEET_ROUTE_PIN=0 turns it off for a caller that owns the config itself.
route_pin() {
  [ "${FLEET_ROUTE_PIN:-1}" = "1" ] || return 0
  [ -n "$FLEET_PY_BIN" ] || return 0
  local pin="${FLEET_ROUTE_PIN_TOOL:-${SCRIPT_DIR}/pin_fleet_route.py}"
  [ -f "$pin" ] || return 0
  # The pin target follows the deployment's dispatcher: fleet.env's
  # FLEET_GATEWAY (this deployment pins CC Switch's gateway) overrides the
  # tool's default, so the guard restores the switcher's own route instead of
  # fighting it.
  local fleet_env="${FLEET_HOME:-$HOME/FleetKit/runtime}/fleet.env"
  if [ -f "$fleet_env" ]; then
    local gw
    gw="$(sed -n 's/^FLEET_GATEWAY=//p' "$fleet_env" | head -1 | tr -d '\r"')"
    [ -n "$gw" ] && export FLEET_GATEWAY="$gw"
  fi
  local extra=""
  [ "$DRY_RUN" = "1" ] && extra="--dry-run"
  # shellcheck disable=SC2086  # $extra is either empty or one flag
  "$FLEET_PY_BIN" "$pin" --config "${CODEX_HOME}/config.toml" \
      --codex-home "$CODEX_HOME" $extra >>"$LOG_FILE" 2>&1 \
      || log "route pin failed on ${CODEX_HOME}/config.toml"
  return 0
}

cmd_run() {
  mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true

  route_pin

  if ! command -v ocx >/dev/null 2>&1; then
    log "skip: ocx not on PATH"
    return 0
  fi

  if ! printf "%s" "$MIN_MODELS" | grep -q "^[0-9][0-9]*$"; then
    log "skip: --min-models is not a number ($MIN_MODELS)"
    return 0
  fi

  counts="$(catalog_bridge_count || true)"
  count="${counts%% *}"
  hidden="${counts##* }"
  if ! printf "%s" "$count" | grep -q "^[0-9][0-9]*$"; then
    # No declaration, or a catalog that no longer parses. This is the worst
    # state, not an idle one, and reporting "skip" here is how a stripped
    # config stayed stripped for as long as the timer ran.
    log "heal: cannot count bridge models ($count), running ocx sync"
    if [ "$DRY_RUN" = "1" ]; then
      log "[dry-run] ocx sync"
      return 0
    fi
    if ocx sync >>"$LOG_FILE" 2>&1; then
      after="$(catalog_bridge_count || echo unknown)"
      log "heal done: $count, now $after"
    else
      log "heal FAILED: ocx sync exited non-zero"
      return 1
    fi
    return 0
  fi

  if [ "$count" -ge "$MIN_MODELS" ]; then
    if [ "${OCX_GUARD_VERBOSE:-0}" = "1" ]; then
      log "ok: $count bridge models in catalog"
    fi
    return 0
  fi

  # Short only because the filter hid verified-not-REAL rows: the catalog is
  # not stripped, and healing it would re-add rows that fail on pick. Only a
  # shortfall the filter cannot explain is worth an ocx sync.
  if [ "$hidden" -gt 0 ] 2>/dev/null \
     && [ $((count + hidden)) -ge "$MIN_MODELS" ]; then
    log "ok: $count bridge models + $hidden hidden by the filter = $((count + hidden)); not healing"
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
  local label="${LABEL_PREFIX}.ocx-catalog-guard"
  local path_value="${CATALOG_FILTER_PATH:-${PATH:-/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin}}"
  local ocx_bin
  ocx_bin="$(command -v ocx 2>/dev/null || true)"
  if [ -n "$ocx_bin" ]; then
    path_value="$(dirname "$ocx_bin"):${path_value}"
  fi
  local ocx_path="$path_value"
  local st
  st="$(command -v bash 2>/dev/null || echo /bin/bash)"
  fleet_timer_install "$label" "$INTERVAL" "$st" "${SCRIPT_DIR}/ocx-catalog-guard.sh" "run"
  echo "installed ${label} (every ${INTERVAL}s) on $(fleet_os 2>/dev/null || echo macos): ${ocx_path}"
}

cmd_uninstall_timer() {
  local label="${LABEL_PREFIX}.ocx-catalog-guard"
  fleet_service_remove "$label"
  echo "removed ${label}"
}

cmd_status() {
  label="${LABEL_PREFIX}.ocx-catalog-guard"
  counts="$(catalog_bridge_count || echo "unknown 0")"
  count="${counts%% *}"
  hidden="${counts##* }"
  echo "codex home  : ${CODEX_HOME}"
  echo "bridge models in catalog: ${count} (+${hidden} hidden by the filter; heal below ${MIN_MODELS})"
  echo "log         : ${LOG_FILE}"
  # Verify the real schedule, not just that a definition file exists. A launchd
  # plist with only RunAtLoad looks installed but fires once at login and never
  # again, which is how the reverse-proxied models went missing after a restart.
  plist="${LAUNCH_DIR}/${label}.plist"
  if [ -f "$plist" ]; then
    interval_kv="$(awk '/<key>StartInterval<\/key>/{f=1;next} f&&/<integer>/{gsub(/<[^>]*>/,"");sub(/^ +/,"");sub(/ +$/,"");print;exit}' "$plist")"
    if [ -n "$interval_kv" ] && [ "$interval_kv" -gt 0 ] 2>/dev/null; then
      echo "timer       : installed (every ${interval_kv}s)"
    else
      echo "timer       : BROKEN (plist has no StartInterval; re-run install-timer)"
    fi
  elif fleet_service_exists "$label"; then
    echo "timer       : installed (every ${INTERVAL}s, $(fleet_os 2>/dev/null || echo macos))"
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

#!/usr/bin/env bash
# Install a periodic timer that re-measures fleet reachability.
# macOS uses a launchd StartInterval plist, Windows a repeating scheduled
# task, Linux a respawning wrapper that sleeps INTERVAL between runs.
#
# Why a timer instead of an inline call: a full probe makes a real chat call
# per bridge and takes minutes, so it must never block `ocx sync`. The wrapper
# (fleet-sort-after-sync.sh) only kicks this timer when the snapshot is stale.
#
#   tools/fleet-probe-install.sh            install/update + start
#   tools/fleet-probe-install.sh status     show state
#   tools/fleet-probe-install.sh run        probe once in the foreground
#   tools/fleet-probe-install.sh uninstall  remove the timer
set -euo pipefail

KIT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Platform abstraction: launchd / Task Scheduler / Linux supervisor.
if [ -f "$KIT/tools/platform.sh" ]; then
  # shellcheck source=platform.sh
  . "$KIT/tools/platform.sh"
fi
LABEL="${FLEET_LABEL_PREFIX:-com.local}.fleet-probe"
SERVICE_DIR="$(fleet_service_dir 2>/dev/null || echo "${FLEET_LAUNCH_DIR:-$HOME/Library/LaunchAgents}")"
SERVICE_DIR="${FLEET_LAUNCH_DIR:-${SERVICE_DIR}}"
FLEET_SERVICE_DIR="${FLEET_SERVICE_DIR:-${SERVICE_DIR}}"
export FLEET_SERVICE_DIR
PLIST="${SERVICE_DIR}/${LABEL}.plist"
PYTHON="${FLEET_PYTHON:-}"
if [ -z "$PYTHON" ] || [ ! -x "$PYTHON" ]; then
  PYTHON="$(fleet_venv_python "$KIT" 2>/dev/null || true)"
  [ -n "$PYTHON" ] && [ -x "$PYTHON" ] || PYTHON="$(command -v python3 || command -v python)"
fi
PROBE="$KIT/tools/fleet_probe.py"
REACH="${FLEET_REACH_FILE:-$HOME/.codex/fleet-reach.json}"
ENV_FILE="${FLEET_ENV_FILE:-$KIT/../runtime/fleet.env}"
ENV_FILE="$(cd "$(dirname "$ENV_FILE")" && pwd)/$(basename "$ENV_FILE")"  # absolute: launchd dislikes ..
LOG="${FLEET_PROBE_LOG:-$(fleet_log_dir 2>/dev/null || echo /tmp/fleet-logs)/fleet-probe.log}"
INTERVAL="${FLEET_PROBE_INTERVAL:-1800}"

install_timer() {
    mkdir -p "$SERVICE_DIR" "$(dirname "$LOG")"
    # --env matters: the generated wrapper exports no FLEET_HOME, so the
    # probe has to be told where the fleet env lives
    fleet_timer_install "$LABEL" "$INTERVAL" "$PYTHON" "$PROBE" \
        "--env $ENV_FILE --out $REACH --tries 6 --call-timeout 45 --sort-after"
    echo "installed $LABEL every ${INTERVAL}s on $(fleet_os 2>/dev/null || echo macos)"
    echo "  service dir: $SERVICE_DIR"
    echo "  log        : $LOG"
}

uninstall_timer() {
    fleet_service_remove "$LABEL"
    echo "removed $LABEL"
}

status_timer() {
    # `set -e` plus `grep -q` in a pipeline aborts the function, so capture first
    local st
    st="$(fleet_service_status "$LABEL" 2>/dev/null || echo missing)"
    case "$st" in
        running) echo "$LABEL: loaded and running" ;;
        ready)   echo "$LABEL: loaded" ;;
        *)       echo "$LABEL: not loaded" ;;
    esac
    if [ -f "$REACH" ]; then
        echo "  snapshot: $REACH"
        "$PYTHON" -c "import json,sys;d=json.load(open(sys.argv[1],encoding=\"utf-8\"));print(\"    measured_at:\",d.get(\"measured_at\"));print(\"    reachable  :\",\", \".join(d.get(\"reachable\") or []));print(\"    unreachable:\",\", \".join(d.get(\"unreachable\") or []))" "$REACH"
    else
        echo "  snapshot: missing ($REACH)"
    fi
}

run_once() {
    FLEET_ENV_FILE="$ENV_FILE" "$PYTHON" "$PROBE" --out "$REACH" \
        --tries 6 --call-timeout 8
}

case "${1:-install}" in
    install)   install_timer ;;
    uninstall) uninstall_timer ;;
    status)    status_timer ;;
    run)       run_once ;;
    *) echo "usage: $0 [install|uninstall|status|run]" >&2; exit 2 ;;
esac

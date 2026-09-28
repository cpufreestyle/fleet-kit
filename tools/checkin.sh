#!/usr/bin/env bash
# FleetKit auto check-in control (daily points for subscription platforms).
#
# Wraps tools/checkin.py (task registry; currently xhx SenseTime Raccoon).
# The WorkBuddy "Buddy 加油站" check-in is bridge-integrated (account pool,
# dashboard) and is NOT handled here.
#
# Usage: checkin.sh <command> [--home DIR]
#   status             show today state and balances (no network)
#   run-now [--force]  run all tasks now (--force ignores today success)
#   install-timer      install the daily 09:00 CST launchd timer
#   uninstall-timer    remove the timer
set -euo pipefail

CMD=""
ARG_HOME=""
PASSTHRU=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    -h|--help)
      echo "Usage: checkin.sh <status|run-now|install-timer|uninstall-timer> [--home DIR] [--force]"
      exit 0
      ;;
    --home)
      shift
      [ "$#" -gt 0 ] || { echo "--home requires a value" >&2; exit 1; }
      ARG_HOME="$1"
      ;;
    --home=*) ARG_HOME="$(echo "$1" | cut -d= -f2-)" ;;
    --force) PASSTHRU+=("--force") ;;
    status|run-now|install-timer|uninstall-timer)
      if [ -z "$CMD" ]; then CMD="$1"; else echo "unexpected argument: $1" >&2; exit 1; fi
      ;;
    *) PASSTHRU+=("$1") ;;
  esac
  shift
done

if [ -z "$CMD" ]; then
  echo "command required: status | run-now | install-timer | uninstall-timer" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
FLEET_HOME="$ARG_HOME"
if [ -z "$FLEET_HOME" ]; then
  FLEET_HOME="$(cd "$SCRIPT_DIR/.." && pwd)"
fi

ENVFILE="$FLEET_HOME/fleet.env"
if [ -f "$ENVFILE" ]; then
  set -a
  . "$ENVFILE"
  set +a
fi

CHECKIN_HOME="${CODEX_CHECKIN_HOME:-$FLEET_HOME/checkin}"
CHECKIN_PY="$SCRIPT_DIR/checkin.py"
if [ ! -f "$CHECKIN_PY" ]; then
  echo "checkin.py not found next to this script ($SCRIPT_DIR)" >&2
  exit 1
fi

PY="${FLEET_PYTHON:-}"
if [ -z "$PY" ] || [ ! -x "$PY" ]; then
  if [ -x "$FLEET_HOME/.venv/bin/python" ]; then
    PY="$FLEET_HOME/.venv/bin/python"
  else
    PY="$(command -v python3 || true)"
  fi
fi
if [ -z "$PY" ]; then
  echo "python3 not found in PATH" >&2
  exit 1
fi

LABEL_PREFIX="${LABEL_PREFIX:-com.local}"
# Platform abstraction: launchd / Task Scheduler / Linux supervisor.
if [ -f "$FLEET_HOME/tools/platform.sh" ]; then
  # shellcheck source=tools/platform.sh
  . "$FLEET_HOME/tools/platform.sh"
fi
if command -v fleet_service_dir >/dev/null 2>&1; then LAUNCH_DIR="$(fleet_service_dir)"; fi
if command -v fleet_log_dir >/dev/null 2>&1; then LOG_DIR="$(fleet_log_dir)"; fi
LAUNCH_DIR="${LAUNCH_DIR:-$HOME/Library/LaunchAgents}"
LOG_DIR="${LOG_DIR:-/tmp/fleet-logs}"
FLEET_SERVICE_DIR="${FLEET_SERVICE_DIR:-$LAUNCH_DIR}"
export FLEET_SERVICE_DIR FLEET_LOG_DIR="$LOG_DIR"
CHECKIN_LABEL="${LABEL_PREFIX}.fleet-checkin"

case "$CMD" in
  status)
    CODEX_CHECKIN_HOME="$CHECKIN_HOME" "$PY" "$CHECKIN_PY" --status
    ;;
  run-now)
    CODEX_CHECKIN_HOME="$CHECKIN_HOME" "$PY" "$CHECKIN_PY" --run-now ${PASSTHRU[@]+"${PASSTHRU[@]}"}
    ;;
  install-timer)
    mkdir -p "$LAUNCH_DIR" "$CHECKIN_HOME" "$LOG_DIR"
    fleet_timer_daily "$CHECKIN_LABEL" "09:00" "$PY" "$CHECKIN_PY" "--daemon"
    echo "installed ${CHECKIN_LABEL}: daily 09:00 CST, state ${CHECKIN_HOME}, log ${LOG_DIR}/${CHECKIN_LABEL}.log"
    ;;
  uninstall-timer)
    fleet_service_remove "${CHECKIN_LABEL}"
    echo "removed ${CHECKIN_LABEL}"
    ;;
esac

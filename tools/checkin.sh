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
  PY="$(command -v python3 || true)"
fi
if [ -z "$PY" ]; then
  echo "python3 not found in PATH" >&2
  exit 1
fi

LABEL_PREFIX="${LABEL_PREFIX:-com.local}"
LAUNCH_DIR="${LAUNCH_DIR:-$HOME/Library/LaunchAgents}"
LOG_DIR="${LOG_DIR:-/tmp/fleet-logs}"
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
    plist="$LAUNCH_DIR/${CHECKIN_LABEL}.plist"
    cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key>
    <string>${HOME}</string>
    <key>PATH</key>
    <string>${PATH}</string>
    <key>CODEX_CHECKIN_HOME</key>
    <string>${CHECKIN_HOME}</string>
  </dict>
  <key>Label</key>
  <string>${CHECKIN_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${PY}</string>
    <string>${CHECKIN_PY}</string>
    <string>--daemon</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>
    <integer>9</integer>
    <key>Minute</key>
    <integer>0</integer>
  </dict>
  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/checkin.log</string>
  <key>StandardOutPath</key>
  <string>${LOG_DIR}/checkin.log</string>
  <key>WorkingDirectory</key>
  <string>${FLEET_HOME}</string>
</dict>
</plist>
PLIST
    launchctl bootout "gui/$(id -u)/${CHECKIN_LABEL}" >/dev/null 2>&1 || true
    launchctl bootstrap "gui/$(id -u)" "$plist"
    echo "installed ${CHECKIN_LABEL}: daily 09:00 CST, state ${CHECKIN_HOME}, log ${LOG_DIR}/checkin.log"
    ;;
  uninstall-timer)
    launchctl bootout "gui/$(id -u)/${CHECKIN_LABEL}" >/dev/null 2>&1 || true
    plist="$LAUNCH_DIR/${CHECKIN_LABEL}.plist"
    if [ -f "$plist" ]; then
      unlink "$plist" 2>/dev/null || true
      echo "removed $plist"
    fi
    echo "removed ${CHECKIN_LABEL}"
    ;;
esac

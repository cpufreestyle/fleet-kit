#!/usr/bin/env bash
# fleet-kit status UI control: local web dashboard for the nine bridges.
#
# The dashboard itself is tools/status_ui.py (stdlib only, no new dependency).
# This wrapper only manages its lifecycle, in the same shape as checkin.sh.
#
# Usage: status_ui.sh <command> [--home DIR]
#   run               run the dashboard in the foreground (Ctrl-C to stop)
#   start             run it in the background (pidfile)
#   stop              stop the background instance
#   status            show URL, pid and port
#   install-timer     install a launchd agent that keeps the dashboard up
#   uninstall-timer   remove that launchd agent
set -euo pipefail

CMD=""
ARG_HOME=""
EXTRA=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -h|--help)
      echo "Usage: status_ui.sh <run|start|stop|status|install-timer|uninstall-timer> [--home DIR]"
      exit 0
      ;;
    --home)
      shift
      [ "$#" -gt 0 ] || { echo "--home requires a value" >&2; exit 1; }
      ARG_HOME="$1"
      ;;
    --home=*) ARG_HOME="$(echo "$1" | cut -d= -f2-)" ;;
    --no-browser) EXTRA="$EXTRA --no-browser" ;;
    run|start|stop|status|install-timer|uninstall-timer)
      if [ -z "$CMD" ]; then CMD="$1"; else echo "unexpected argument: $1" >&2; exit 1; fi
      ;;
    *) EXTRA="$EXTRA $1" ;;
  esac
  shift
done

if [ -z "$CMD" ]; then
  echo "command required: run | start | stop | status | install-timer | uninstall-timer" >&2
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

# fleet.env is optional, so FLEET_PYTHON may be unset under set -u.
set +u
PY="$FLEET_PYTHON"
if [ -z "$PY" ] || [ ! -x "$PY" ]; then
  PY="$(command -v python3 || true)"
fi
set -u
if [ -z "$PY" ] || [ ! -x "$PY" ]; then
  echo "python3 not found in PATH" >&2
  exit 1
fi

UI_PY="$SCRIPT_DIR/status_ui.py"
if [ ! -f "$UI_PY" ]; then
  echo "status_ui.py not found next to this script ($SCRIPT_DIR)" >&2
  exit 1
fi

# fleet.env is optional, so these variables may be unset under set -u.
set +u
if [ -z "$PORT_BASE" ]; then PORT_BASE=8787; fi
if [ -z "$LABEL_PREFIX" ]; then LABEL_PREFIX=com.local; fi
if [ -z "$LAUNCH_DIR" ]; then LAUNCH_DIR="$HOME/Library/LaunchAgents"; fi
if [ -z "$LOG_DIR" ]; then LOG_DIR=/tmp/fleet-logs; fi
if [ -z "$UI_PORT" ]; then UI_PORT=$((PORT_BASE + 9)); fi
set -u

UI_LABEL="$LABEL_PREFIX.fleet-ui"
UI_PIDFILE="$LOG_DIR/status-ui.pid"
UI_URL="http://127.0.0.1:$UI_PORT/"

ui_pid() {
  if [ -f "$UI_PIDFILE" ] && kill -0 "$(cat "$UI_PIDFILE")" 2>/dev/null; then
    cat "$UI_PIDFILE"
    return 0
  fi
  return 1
}

install_ui_plist() {
  mkdir -p "$LAUNCH_DIR" "$LOG_DIR"
  plist="$LAUNCH_DIR/$UI_LABEL.plist"
  cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key>
    <string>$HOME</string>
    <key>PATH</key>
    <string>$PATH</string>
  </dict>
  <key>KeepAlive</key>
  <true/>
  <key>Label</key>
  <string>$UI_LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$UI_PY</string>
    <string>--port</string>
    <string>$UI_PORT</string>
    <string>--home</string>
    <string>$FLEET_HOME</string>
    <string>--no-browser</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>StandardErrorPath</key>
  <string>$LOG_DIR/status-ui.log</string>
  <key>StandardOutPath</key>
  <string>$LOG_DIR/status-ui.log</string>
  <key>WorkingDirectory</key>
  <string>$FLEET_HOME</string>
</dict>
</plist>
PLIST
  launchctl bootout "gui/$(id -u)/$UI_LABEL" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$(id -u)" "$plist"
  echo "installed $UI_LABEL: $UI_URL  log $LOG_DIR/status-ui.log"
}

remove_ui_plist() {
  launchctl bootout "gui/$(id -u)/$UI_LABEL" >/dev/null 2>&1 || true
  plist="$LAUNCH_DIR/$UI_LABEL.plist"
  if [ -f "$plist" ]; then
    unlink "$plist" 2>/dev/null || true
    echo "removed $plist"
  fi
  echo "removed $UI_LABEL"
}

case "$CMD" in
  run)
    exec "$PY" "$UI_PY" --home "$FLEET_HOME" $EXTRA
    ;;
  start)
    if pid="$(ui_pid)"; then
      echo "already running (pid $pid): $UI_URL"
      exit 0
    fi
    mkdir -p "$LOG_DIR"
    nohup "$PY" "$UI_PY" --home "$FLEET_HOME" --no-browser $EXTRA \
      >>"$LOG_DIR/status-ui.log" 2>&1 &
    echo $! > "$UI_PIDFILE"
    sleep 1
    echo "started (pid $(cat "$UI_PIDFILE")): $UI_URL  log $LOG_DIR/status-ui.log"
    ;;
  stop)
    if pid="$(ui_pid)"; then
      kill "$pid" 2>/dev/null || true
      echo "stopped (pid $pid)"
    else
      echo "not running (no live pidfile $UI_PIDFILE)"
    fi
    unlink "$UI_PIDFILE" 2>/dev/null || true
    ;;
  status)
    if pid="$(ui_pid)"; then
      echo "running (pid $pid): $UI_URL"
    elif launchctl print "gui/$(id -u)/$UI_LABEL" >/dev/null 2>&1; then
      echo "launchd agent $UI_LABEL loaded: $UI_URL"
    else
      echo "stopped: $UI_URL (start it: bash $0 start)"
    fi
    ;;
  install-timer)
    install_ui_plist
    ;;
  uninstall-timer)
    remove_ui_plist
    ;;
esac

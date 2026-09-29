#!/usr/bin/env bash
# FleetKit StepFun image-cap shim control.
#
# tools/stepfun_image_shim.py is a transparent pass-through in front of CC
# Switch (127.0.0.1:15721) that de-duplicates and caps the photos in a Codex
# request before StepFun's Plan API hits its 70-image ceiling (see
# tools/image_cap.py for the measurement). It listens one port above CC Switch
# on 15722, so Codex's base_url must point at the shim, not at CC Switch.
# Every setting comes from the IMAGE_CAP_* environment, which keeps this
# wrapper and the launchd plist it writes argument-free and identical.
#
# Usage: stepfun_image_shim.sh <command> [--home DIR]
#   run               run the shim in the foreground (Ctrl-C to stop)
#   start             run it in the background (pidfile)
#   stop              stop the background instance
#   status            show URL, pid, port and a health probe
#   install-timer     install a launchd agent that keeps the shim up
#   uninstall-timer   remove that launchd agent
set -euo pipefail

CMD=""
ARG_HOME=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -h|--help)
      echo "Usage: stepfun_image_shim.sh <run|start|stop|status|install-timer|uninstall-timer> [--home DIR]"
      exit 0
      ;;
    --home)
      shift
      [ "$#" -gt 0 ] || { echo "--home requires a value" >&2; exit 1; }
      ARG_HOME="$1"
      ;;
    --home=*) ARG_HOME="$(echo "$1" | cut -d= -f2-)" ;;
    run|start|stop|status|install-timer|uninstall-timer)
      if [ -z "$CMD" ]; then CMD="$1"; else echo "unexpected argument: $1" >&2; exit 1; fi
      ;;
    *) echo "unexpected argument: $1" >&2; exit 1 ;;
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

SHIM_PY="$SCRIPT_DIR/stepfun_image_shim.py"
if [ ! -f "$SHIM_PY" ]; then
  echo "stepfun_image_shim.py not found next to this script ($SCRIPT_DIR)" >&2
  exit 1
fi

# fleet.env is optional, so these variables may be unset under set -u.
set +u
# Platform abstraction: launchd / Task Scheduler / Linux supervisor.
if [ -f "$FLEET_HOME/tools/platform.sh" ]; then
  # shellcheck source=tools/platform.sh
  . "$FLEET_HOME/tools/platform.sh"
fi
if [ -z "$PORT_BASE" ]; then PORT_BASE=8787; fi
if [ -z "$LABEL_PREFIX" ]; then LABEL_PREFIX=com.local; fi
if command -v fleet_service_dir >/dev/null 2>&1; then LAUNCH_DIR="$(fleet_service_dir)"; fi
if command -v fleet_log_dir >/dev/null 2>&1; then LOG_DIR="$(fleet_log_dir)"; fi
if [ -z "$LAUNCH_DIR" ]; then LAUNCH_DIR="$HOME/Library/LaunchAgents"; fi
if [ -z "$LOG_DIR" ]; then LOG_DIR=/tmp/fleet-logs; fi
FLEET_SERVICE_DIR="${FLEET_SERVICE_DIR:-$LAUNCH_DIR}"
export FLEET_SERVICE_DIR FLEET_LOG_DIR="$LOG_DIR"
set -u

# Defaults mirror parse_args() in stepfun_image_shim.py; fleet.env may override.
SHIM_HOST="${IMAGE_CAP_HOST:-127.0.0.1}"
SHIM_PORT="${IMAGE_CAP_PORT:-15722}"
SHIM_UPSTREAM="${IMAGE_CAP_UPSTREAM:-http://127.0.0.1:15721}"
SHIM_MAX="${IMAGE_CAP_MAX:-32}"
SHIM_MODELS="${IMAGE_CAP_MODELS:-step}"
SHIM_LABEL="${LABEL_PREFIX}.stepfun-image-cap"
SHIM_PIDFILE="$LOG_DIR/stepfun-image-cap.pid"
# platform.sh points the launchd job's StandardOutPath/StandardErrorPath at
# $LOG_DIR/<label>.log, so the run/start branches must log to that same file:
# a second, always-empty log name is how the launchd log looked blank.
SHIM_LOG="$LOG_DIR/${SHIM_LABEL}.log"
SHIM_BASE_URL="http://127.0.0.1:$SHIM_PORT"
SHIM_HEALTH_URL="$SHIM_BASE_URL/__image_cap/health"
# launchd reads these from the plist EnvironmentVariables so the plist and the
# run/start branches below start the same python, configured the same way.
ENVPAIRS="IMAGE_CAP_HOST=$SHIM_HOST;IMAGE_CAP_PORT=$SHIM_PORT;IMAGE_CAP_UPSTREAM=$SHIM_UPSTREAM;IMAGE_CAP_MAX=$SHIM_MAX;IMAGE_CAP_MODELS=$SHIM_MODELS"

shim_pid() {
  if [ -f "$SHIM_PIDFILE" ] && kill -0 "$(cat "$SHIM_PIDFILE")" 2>/dev/null; then
    cat "$SHIM_PIDFILE"
    return 0
  fi
  return 1
}

install_shim_plist() {
  mkdir -p "$LAUNCH_DIR" "$LOG_DIR"
  fleet_service_install "$SHIM_LABEL" "$FLEET_HOME" "$ENVPAIRS" "$PY" "$SHIM_PY" ""
  echo "installed $SHIM_LABEL: $SHIM_BASE_URL -> $SHIM_UPSTREAM  log $SHIM_LOG"
}

remove_shim_plist() {
  fleet_service_remove "$SHIM_LABEL"
  echo "removed $SHIM_LABEL"
}

case "$CMD" in
  run)
    export IMAGE_CAP_HOST="$SHIM_HOST" IMAGE_CAP_PORT="$SHIM_PORT" IMAGE_CAP_UPSTREAM="$SHIM_UPSTREAM" IMAGE_CAP_MAX="$SHIM_MAX" IMAGE_CAP_MODELS="$SHIM_MODELS"
    exec "$PY" "$SHIM_PY"
    ;;
  start)
    if pid="$(shim_pid)"; then
      echo "already running (pid $pid): $SHIM_BASE_URL"
      exit 0
    fi
    mkdir -p "$LOG_DIR"
    export IMAGE_CAP_HOST="$SHIM_HOST" IMAGE_CAP_PORT="$SHIM_PORT" IMAGE_CAP_UPSTREAM="$SHIM_UPSTREAM" IMAGE_CAP_MAX="$SHIM_MAX" IMAGE_CAP_MODELS="$SHIM_MODELS"
    nohup "$PY" "$SHIM_PY" >>"$SHIM_LOG" 2>&1 &
    echo $! > "$SHIM_PIDFILE"
    sleep 1
    echo "started (pid $(cat "$SHIM_PIDFILE")): $SHIM_BASE_URL -> $SHIM_UPSTREAM  log $SHIM_LOG"
    ;;
  stop)
    if pid="$(shim_pid)"; then
      kill "$pid" 2>/dev/null || true
      echo "stopped (pid $pid)"
    else
      echo "not running (no live pidfile $SHIM_PIDFILE)"
    fi
    unlink "$SHIM_PIDFILE" 2>/dev/null || true
    ;;
  status)
    state="$(fleet_service_status "$SHIM_LABEL" 2>/dev/null || true)"
    if pid="$(shim_pid)"; then
      echo "running (pid $pid): $SHIM_BASE_URL -> $SHIM_UPSTREAM"
    elif [ "$state" = "running" ] || [ "$state" = "ready" ]; then
      echo "launchd service $SHIM_LABEL installed ($state): $SHIM_BASE_URL -> $SHIM_UPSTREAM"
    else
      echo "stopped: $SHIM_BASE_URL (start it: bash $0 start)"
    fi
    if curl_health="$(curl --noproxy '*' -s --max-time 2 "$SHIM_HEALTH_URL" 2>/dev/null)"; then
      echo "health: $curl_health"
    fi
    ;;
  install-timer)
    install_shim_plist
    ;;
  uninstall-timer)
    remove_shim_plist
    ;;
esac

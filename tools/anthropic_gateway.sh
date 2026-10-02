#!/usr/bin/env bash
# FleetKit Anthropic Messages gateway control.
#
# tools/anthropic_gateway.py is a stdlib-only Anthropic Messages front end so
# Claude Code desktop can reach the whole catalog through one ANTHROPIC_BASE_URL
# instead of a single provider. The chain is
#
#     Claude Code -> 127.0.0.1:8801 (this gateway)
#                -> 8787..8800 bridges / api.stepfun.com / ocx:10100
#
# The gateway never swaps a model behind the caller's back: a name it does not
# know is a 404, and a target that went down is reported through the
# x-fleetkit-resolved-model header (see resolve() in anthropic_gateway.py).
#
# Usage: anthropic_gateway.sh <command> [--home DIR]
#   run             run the gateway in the foreground (Ctrl-C to stop)
#   start           run it in the background (pidfile)
#   stop            stop the background instance
#   status          show URL, pid, port and a health probe
#   install         install a persistent service (launchd / systemd / Task Scheduler)
#   uninstall       remove that service
#   check           one real end-to-end call per route, then exit
#   print-config    dump the resolved routes and keys, then exit
set -euo pipefail

CMD=""
ARG_HOME=""
EXTRA=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    -h|--help)
      echo "Usage: anthropic_gateway.sh <run|start|stop|status|install|uninstall|check|print-config> [--home DIR]"
      exit 0
      ;;
    --home)
      shift
      [ "$#" -gt 0 ] || { echo "--home requires a value" >&2; exit 1; }
      ARG_HOME="$1"
      ;;
    --home=*) ARG_HOME="${1#--home=}" ;;
    run|start|stop|status|install|uninstall|check|print-config)
      if [ -z "$CMD" ]; then CMD="$1"; else EXTRA+=("$1"); fi
      ;;
    *)
      # everything after the command belongs to it: "check <slug>..." is how
      # a single route gets a real end-to-end call
      if [ -n "$CMD" ]; then EXTRA+=("$1"); else echo "unexpected argument: $1" >&2; exit 1; fi
      ;;
  esac
  shift
done

if [ -z "$CMD" ]; then
  echo "command required: run | start | stop | status | install | uninstall | check | print-config" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
FLEET_HOME="$ARG_HOME"
if [ -z "$FLEET_HOME" ]; then
  FLEET_HOME="$(cd "$SCRIPT_DIR/.." && pwd)"
fi

# fleet.env is optional: a checkout without keys still wants the wrapper to run
# and report its open routes rather than exit before printing anything. It
# lives at <repo>/runtime/fleet.env, not next to kit/, and the gateway's
# StepFun direct route reads its key from there (the bridges keep theirs in
# their own service files, so only the direct route exposes a wrong path).
# Take the first candidate that exists and leave ENVFILE empty otherwise, so
# the python side falls back to its own default instead of a dead path.
ENVFILE=""
for candidate in "$FLEET_HOME/fleet.env"                  "$FLEET_HOME/../runtime/fleet.env"                  "$HOME/AI Shared/repo/FleetKit/runtime/fleet.env"; do
  if [ -f "$candidate" ]; then
    ENVFILE="$candidate"
    break
  fi
done
if [ -n "$ENVFILE" ]; then
  set -a
  . "$ENVFILE"
  set +a
fi

# fleet.env is optional, so FLEET_PYTHON may be unset under set -u.
set +u
PY="$FLEET_PYTHON"
if [ -z "$PY" ] || [ ! -x "$PY" ]; then
  PY="$FLEET_HOME/../runtime/.venv/bin/python"
fi
if [ -z "$PY" ] || [ ! -x "$PY" ]; then
  PY="$(command -v python3 || true)"
fi
set -u
if [ -z "$PY" ] || [ ! -x "$PY" ]; then
  echo "python3 not found in PATH (or set FLEET_PYTHON in fleet.env)" >&2
  exit 1
fi

GATEWAY_PY="$SCRIPT_DIR/anthropic_gateway.py"
if [ ! -f "$GATEWAY_PY" ]; then
  echo "anthropic_gateway.py not found next to this script ($SCRIPT_DIR)" >&2
  exit 1
fi

# fleet.env is optional, so these variables may be unset under set -u.
set +u
# Platform abstraction: launchd / systemd wrapper / Task Scheduler.
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
set -u

# Defaults mirror anthropic_gateway.py (DEFAULT_PORT and the CATALOG_PATH
# default); fleet.env or the caller's environment may override them.
GW_HOST="${FLEET_ANTHROPIC_HOST:-127.0.0.1}"
GW_PORT="${FLEET_ANTHROPIC_PORT:-8801}"
GW_TIMEOUT="${FLEET_ANTHROPIC_TIMEOUT:-240}"
GW_CATALOG="${FLEET_ANTHROPIC_CATALOG:-$HOME/.codex/cc-switch-model-catalog.json}"
GW_LABEL="${LABEL_PREFIX}.fleet-anthropic"
GW_PIDFILE="$LOG_DIR/fleet-anthropic.pid"
# platform.sh points the service job's StandardOutPath/StandardErrorPath at
# $LOG_DIR/<label>.log, so the run/start branches must log to that same file:
# a second, always-empty log name is how the launchd log looked blank.
GW_LOG="$LOG_DIR/${GW_LABEL}.log"
GW_BASE_URL="http://$GW_HOST:$GW_PORT"
GW_HEALTH_URL="$GW_BASE_URL/health"
# GW_ARGS is deliberately unquoted: it is a fixed host/port/timeout triple with
# no spaces in it, and quoting it would hand argparse one unrecognised blob.
GW_ARGS="--host $GW_HOST --port $GW_PORT --timeout $GW_TIMEOUT"

# The optional bearer token. Unset means the gateway answers on loopback only,
# which is the same trust rule the rest of FleetKit already runs on.
GW_TOKEN="${FLEET_ANTHROPIC_TOKEN:-}"

# ';'-separated for platform.sh (it splits on ';' only, so a path with a space
# survives) and absolute for launchd, which does not expand a '~'.
ENVPAIRS="FLEET_ANTHROPIC_HOST=$GW_HOST;FLEET_ANTHROPIC_PORT=$GW_PORT;FLEET_ANTHROPIC_TIMEOUT=$GW_TIMEOUT;FLEET_ANTHROPIC_CATALOG=$GW_CATALOG"
if [ -n "$ENVFILE" ]; then
  ENVPAIRS="$ENVPAIRS;FLEET_ENV_FILE=$ENVFILE"
fi
if [ -n "$GW_TOKEN" ]; then
  ENVPAIRS="$ENVPAIRS;FLEET_ANTHROPIC_TOKEN=$GW_TOKEN"
fi

# What run/start export. The paths are one entry per line because a
# space-separated list would word-split FLEET_HOME, which contains a space.
EXPORTS=()
EXPORTS+=("FLEET_ANTHROPIC_HOST=$GW_HOST")
EXPORTS+=("FLEET_ANTHROPIC_PORT=$GW_PORT")
EXPORTS+=("FLEET_ANTHROPIC_TIMEOUT=$GW_TIMEOUT")
EXPORTS+=("FLEET_ANTHROPIC_CATALOG=$GW_CATALOG")
if [ -n "$ENVFILE" ]; then
  EXPORTS+=("FLEET_ENV_FILE=$ENVFILE")
fi
if [ -n "$GW_TOKEN" ]; then
  EXPORTS+=("FLEET_ANTHROPIC_TOKEN=$GW_TOKEN")
fi

gw_pid() {
  if [ -f "$GW_PIDFILE" ] && kill -0 "$(cat "$GW_PIDFILE")" 2>/dev/null; then
    cat "$GW_PIDFILE"
    return 0
  fi
  return 1
}


case "$CMD" in
  run)
    for kv in "${EXPORTS[@]}"; do export "$kv"; done
    exec "$PY" "$GATEWAY_PY" $GW_ARGS
    ;;
  start)
    if pid="$(gw_pid)"; then
      echo "already running (pid $pid): $GW_BASE_URL"
      exit 0
    fi
    mkdir -p "$LOG_DIR"
    for kv in "${EXPORTS[@]}"; do export "$kv"; done
    nohup "$PY" "$GATEWAY_PY" $GW_ARGS >>"$GW_LOG" 2>&1 &
    echo $! > "$GW_PIDFILE"
    sleep 1
    if ! kill -0 "$(cat "$GW_PIDFILE")" 2>/dev/null; then
      echo "gateway exited immediately, last log lines:" >&2
      tail -n 20 "$GW_LOG" >&2 || true
      exit 1
    fi
    echo "started (pid $(cat "$GW_PIDFILE")): $GW_BASE_URL  log $GW_LOG"
    ;;
  stop)
    if pid="$(gw_pid)"; then
      kill "$pid" 2>/dev/null || true
      echo "stopped (pid $pid)"
    else
      echo "not running (no live pidfile $GW_PIDFILE)"
    fi
    ;;
  status)
    state="$(fleet_service_status "$GW_LABEL" 2>/dev/null || true)"
    if pid="$(gw_pid)"; then
      echo "running (pid $pid): $GW_BASE_URL"
    elif [ "$state" = "running" ] || [ "$state" = "ready" ]; then
      echo "service $GW_LABEL installed ($state): $GW_BASE_URL"
    else
      echo "stopped: $GW_BASE_URL (start it: bash $0 start | install)"
    fi
    if health="$(curl --noproxy "*" -s --max-time 3 "$GW_HEALTH_URL" 2>/dev/null)"; then
      echo "health: $health"
    else
      echo "health: no answer from $GW_HEALTH_URL" >&2
    fi
    ;;
  install)
    mkdir -p "$LAUNCH_DIR" "$LOG_DIR"
    fleet_service_install "$GW_LABEL" "$FLEET_HOME" "$ENVPAIRS" "$PY" "$GATEWAY_PY" "$GW_ARGS"
    echo "installed $GW_LABEL: $GW_BASE_URL  log $GW_LOG"
    ;;
  uninstall)
    fleet_service_remove "$GW_LABEL"
    echo "removed $GW_LABEL"
    ;;
  check)
    for kv in "${EXPORTS[@]}"; do export "$kv"; done
    # ${EXTRA[@]+...} keeps an empty list legal under set -u on bash 3.2
    exec "$PY" "$GATEWAY_PY" $GW_ARGS --check ${EXTRA[@]+"${EXTRA[@]}"}
    ;;
  print-config)
    for kv in "${EXPORTS[@]}"; do export "$kv"; done
    exec "$PY" "$GATEWAY_PY" $GW_ARGS --print-config
    ;;
esac

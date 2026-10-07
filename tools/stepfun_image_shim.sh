#!/usr/bin/env bash
# FleetKit StepFun image-cap shim control.
#
# tools/stepfun_image_shim.py is a transparent pass-through below CC Switch
# (127.0.0.1:15721) that de-duplicates and caps the photos in a Codex request
# before StepFun's Plan API hits its 70-image ceiling (see tools/image_cap.py
# for the measurement). The chain is
#
#     Codex -> 15721 (CC Switch) -> 15722 (this shim) -> api.stepfun.com
#
# so CC Switch's StepFun provider has to point at the shim rather than straight
# at StepFun: tools/pin_cc_switch_endpoint.py does that, and the shim re-runs it
# on a timer because a provider row re-added by hand points past it again.
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
#
# The shim occasionally hangs: the process stays alive and the launchd job
# still reports state = running, but the event loop stops answering and every
# StepFun request in flight never comes back. KeepAlive cannot see that -- it
# relaunches a service that exits, and a hung loop never exits -- so there are
# two watchers, one on each side of the process:
#
#   1. the shim itself: a daemon thread probes its own /__image_cap/health
#      over a raw socket (never through the loop it watches) and replaces the
#      process with a fresh copy of itself after IMAGE_CAP_WATCHDOG_STRIKES
#      consecutive failures. Fast, pid-preserving, sees only this process.
#   2. this script, on a launchd timer: `watchdog` probes the same endpoint
#      from outside and force-restarts the service. Slower, but it is the
#      only thing that recovers a whole-process wedge, or a second instance
#      holding the port, which no thread inside the process can see.
#
# Both are best effort and neither touches another service: the restart only
# ever costs the requests already stuck in the hung loop, and the probe is
# slow to accuse (short timeout, two strikes, expiry on old strikes) so a
# busy but healthy shim is never restarted.
#
#   watchdog          one probe-and-restart cycle (the timer runs this)
#   install-watchdog  install the launchd timer that runs `watchdog`
#   uninstall-watchdog  remove that timer
set -euo pipefail

CMD=""
ARG_HOME=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -h|--help)
      echo "Usage: stepfun_image_shim.sh <run|start|stop|status|install-timer|uninstall-timer|watchdog|install-watchdog|uninstall-watchdog> [--home DIR]"
      exit 0
      ;;
    --home)
      shift
      [ "$#" -gt 0 ] || { echo "--home requires a value" >&2; exit 1; }
      ARG_HOME="$1"
      ;;
    --home=*) ARG_HOME="$(echo "$1" | cut -d= -f2-)" ;;
    run|start|stop|status|install-timer|uninstall-timer|watchdog|install-watchdog|uninstall-watchdog)
      if [ -z "$CMD" ]; then CMD="$1"; else echo "unexpected argument: $1" >&2; exit 1; fi
      ;;
    *) echo "unexpected argument: $1" >&2; exit 1 ;;
  esac
  shift
done

if [ -z "$CMD" ]; then
  echo "command required: run | start | stop | status | install-timer | uninstall-timer | watchdog | install-watchdog | uninstall-watchdog" >&2
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
  # A bare python3 on Windows resolves to the Store/WSL stub, which lacks the
  # shim's httpx -- prefer the runtime venv (it exists to carry the bridge
  # dependencies), then a real python, then python3 last.
  for cand in "$HOME/FleetKit/runtime/.venv/Scripts/python.exe" \
              "$(command -v python 2>/dev/null || true)" \
              "$(command -v python3 2>/dev/null || true)"; do
    if [ -n "$cand" ] && [ -x "$cand" ]; then
      PY="$cand"
      break
    fi
  done
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
SHIM_UPSTREAM="${IMAGE_CAP_UPSTREAM:-https://api.stepfun.com/step_plan/v1}"
SHIM_MAX="${IMAGE_CAP_MAX:-32}"
SHIM_MODELS="${IMAGE_CAP_MODELS:-step}"
SHIM_REPIN="${IMAGE_CAP_REPIN_INTERVAL:-0}"
SHIM_CC_DB="${IMAGE_CAP_CC_DB:-$HOME/.cc-switch/cc-switch.db}"
SHIM_CC_PROVIDER="${IMAGE_CAP_CC_PROVIDER:-StepFun}"
SHIM_CC_APP_TYPE="${IMAGE_CAP_CC_APP_TYPE:-codex}"
SHIM_CC_PIN="${IMAGE_CAP_CC_PIN_INTERVAL:-300}"
SHIM_LABEL="${LABEL_PREFIX}.stepfun-image-cap"
SHIM_PIDFILE="$LOG_DIR/stepfun-image-cap.pid"
# platform.sh points the launchd job's StandardOutPath/StandardErrorPath at
# $LOG_DIR/<label>.log, so the run/start branches must log to that same file:
# a second, always-empty log name is how the launchd log looked blank.
SHIM_LOG="$LOG_DIR/${SHIM_LABEL}.log"
SHIM_BASE_URL="http://127.0.0.1:$SHIM_PORT"
SHIM_HEALTH_URL="$SHIM_BASE_URL/__image_cap/health"
# Self-watchdog settings, shared with the python side through IMAGE_CAP_*.
# WATCHDOG_TIMEOUT is the in-process probe (generous: one queued 70-image
# rewrite blocks the loop for seconds without the service being lost);
# PROBE_TIMEOUT is the shell probe, kept short so a timer run never lingers.
SHIM_WATCHDOG="${IMAGE_CAP_WATCHDOG:-1}"
SHIM_WATCHDOG_INTERVAL="${IMAGE_CAP_WATCHDOG_INTERVAL:-30}"
SHIM_WATCHDOG_TIMEOUT="${IMAGE_CAP_WATCHDOG_TIMEOUT:-15}"
SHIM_PROBE_TIMEOUT="${IMAGE_CAP_WATCHDOG_PROBE_TIMEOUT:-8}"
SHIM_WATCHDOG_STRIKES="${IMAGE_CAP_WATCHDOG_STRIKES:-2}"
SHIM_STRIKE_TTL="${IMAGE_CAP_WATCHDOG_STRIKE_TTL:-600}"
SHIM_RESTART_WAIT="${IMAGE_CAP_WATCHDOG_RESTART_WAIT:-20}"
SHIM_WATCHDOG_LABEL="${LABEL_PREFIX}.stepfun-image-cap-watchdog"
SHIM_WATCHDOG_LOG="$LOG_DIR/${SHIM_WATCHDOG_LABEL}.log"
# The strike counter has to outlive one timer run, so it lives in a file:
# "<epoch> <consecutive failures>". A strike older than the TTL (machine
# asleep, timer missed) is not a consecutive failure and starts over.
SHIM_STRIKES_FILE="$LOG_DIR/${SHIM_WATCHDOG_LABEL}.strikes"
# Absolute path to this script: the watchdog restarts the shim by re-invoking
# itself, and a launchd job has no cwd to resolve a relative $0 against.
SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
# launchd reads these from the plist EnvironmentVariables so the plist and the
# run/start branches below start the same python, configured the same way.
ENVPAIRS="IMAGE_CAP_HOST=$SHIM_HOST;IMAGE_CAP_PORT=$SHIM_PORT;IMAGE_CAP_UPSTREAM=$SHIM_UPSTREAM;IMAGE_CAP_MAX=$SHIM_MAX;IMAGE_CAP_MODELS=$SHIM_MODELS"
# The paths are passed absolute on purpose. launchd copies EnvironmentVariables
# verbatim and does not expand a ~ in them, so the Python-side default of
# ~/.cc-switch/cc-switch.db would reach the service as a literal tilde and
# every pin would report "db not found" while the shim itself looked healthy.
ENVPAIRS="$ENVPAIRS;IMAGE_CAP_PIN_CONFIG=${IMAGE_CAP_PIN_CONFIG:-$HOME/.codex/config.toml}"
ENVPAIRS="$ENVPAIRS;IMAGE_CAP_CC_DB=$SHIM_CC_DB;IMAGE_CAP_CC_PROVIDER=$SHIM_CC_PROVIDER;IMAGE_CAP_CC_APP_TYPE=$SHIM_CC_APP_TYPE;IMAGE_CAP_CC_PIN_INTERVAL=$SHIM_CC_PIN;IMAGE_CAP_REPIN_INTERVAL=$SHIM_REPIN"
# The in-process watchdog reads the same knobs; a service started with one
# configuration must not run a different one than the timer beside it.
ENVPAIRS="$ENVPAIRS;IMAGE_CAP_WATCHDOG=$SHIM_WATCHDOG;IMAGE_CAP_WATCHDOG_INTERVAL=$SHIM_WATCHDOG_INTERVAL;IMAGE_CAP_WATCHDOG_TIMEOUT=$SHIM_WATCHDOG_TIMEOUT;IMAGE_CAP_WATCHDOG_STRIKES=$SHIM_WATCHDOG_STRIKES"

# What run/start export. Kept as a single list next to ENVPAIRS so the two cannot
# drift: a foreground run has to mean the same configuration as the launchd
# service, or a bug reproduces in one and not the other.
EXPORTS="IMAGE_CAP_HOST=$SHIM_HOST IMAGE_CAP_PORT=$SHIM_PORT IMAGE_CAP_UPSTREAM=$SHIM_UPSTREAM IMAGE_CAP_MAX=$SHIM_MAX IMAGE_CAP_MODELS=$SHIM_MODELS IMAGE_CAP_CC_DB=$SHIM_CC_DB IMAGE_CAP_CC_PROVIDER=$SHIM_CC_PROVIDER IMAGE_CAP_CC_APP_TYPE=$SHIM_CC_APP_TYPE IMAGE_CAP_CC_PIN_INTERVAL=$SHIM_CC_PIN IMAGE_CAP_REPIN_INTERVAL=$SHIM_REPIN IMAGE_CAP_PIN_CONFIG=${IMAGE_CAP_PIN_CONFIG:-$HOME/.codex/config.toml}"

EXPORTS="$EXPORTS IMAGE_CAP_WATCHDOG=$SHIM_WATCHDOG IMAGE_CAP_WATCHDOG_INTERVAL=$SHIM_WATCHDOG_INTERVAL IMAGE_CAP_WATCHDOG_TIMEOUT=$SHIM_WATCHDOG_TIMEOUT IMAGE_CAP_WATCHDOG_STRIKES=$SHIM_WATCHDOG_STRIKES"

shim_pid() {
  if [ -f "$SHIM_PIDFILE" ] && kill -0 "$(cat "$SHIM_PIDFILE")" 2>/dev/null; then
    cat "$SHIM_PIDFILE"
    return 0
  fi
  return 1
}

# One probe of the health endpoint. Loopback, so --noproxy '*' is mandatory:
# the shim's own docstring records an ambient proxy answering its own 503 for
# a target it would not reach, which would look exactly like a hang.
shim_probe() {
  curl --noproxy '*' -s --max-time "$SHIM_PROBE_TIMEOUT" \
    "$SHIM_HEALTH_URL" >/dev/null 2>&1
}

shim_wait_healthy() {
  local deadline=$(( $(date +%s) + $1 ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if shim_probe; then return 0; fi
    sleep 1
  done
  return 1
}

shim_read_strikes() {
  # echoes "<recorded_epoch> <count>", sanitised: a truncated or hand-edited
  # file must never abort the timer under set -e.
  local recorded="" count=""
  if [ -f "$SHIM_STRIKES_FILE" ]; then
    read -r recorded count < "$SHIM_STRIKES_FILE" 2>/dev/null || true
  fi
  case "${count:-}" in ''|*[!0-9]*) count=0 ;; esac
  case "${recorded:-}" in ''|*[!0-9]*) recorded=0 ;; esac
  printf '%s %s\n' "$recorded" "$count"
}

shim_write_strikes() {
  printf '%s %s\n' "$1" "$2" > "$SHIM_STRIKES_FILE" 2>/dev/null || true
}

shim_restart() {
  # launchd first when the plist is there: kickstart -k force-kills the hung
  # instance even when SIGTERM never lands on a stuck loop. The pidfile branch
  # covers an instance started by `start` (or a plist whose job never booted),
  # and every step is followed by a health wait so the two can never race into
  # a second instance on the same port.
  local self_home=""
  [ -n "$ARG_HOME" ] && self_home="--home $ARG_HOME"
  if fleet_service_exists "$SHIM_LABEL" 2>/dev/null; then
    fleet_service_restart "$SHIM_LABEL" >/dev/null 2>&1 || true
    if shim_wait_healthy "$SHIM_RESTART_WAIT"; then
      echo "recovered after restarting the service $SHIM_LABEL"
      return 0
    fi
    # A pidfile instance may be the one holding the port, which is what keeps
    # the launchd instance from ever binding. Stop it and kick once more.
    # shellcheck disable=SC2086  # $self_home is empty or two whole words
    bash "$SELF" $self_home stop >/dev/null 2>&1 || true
    fleet_service_restart "$SHIM_LABEL" >/dev/null 2>&1 || true
    if shim_wait_healthy "$SHIM_RESTART_WAIT"; then
      echo "recovered after stopping the pidfile instance and restarting the service"
      return 0
    fi
    echo "service restart did not recover the shim; trying the pidfile start"
  fi
  # shellcheck disable=SC2086  # $self_home is empty or two whole words
  bash "$SELF" $self_home stop >/dev/null 2>&1 || true
  # shellcheck disable=SC2086  # $self_home is empty or two whole words
  bash "$SELF" $self_home start >/dev/null 2>&1 || true
  if shim_wait_healthy "$SHIM_RESTART_WAIT"; then
    echo "recovered after restarting the background instance"
  else
    echo "still unhealthy after the restart; the next timer run tries again"
  fi
  return 0
}

watchdog_cycle() {
  local now recorded count strikes
  if [ "$SHIM_WATCHDOG" = "0" ]; then
    return 0
  fi
  if ! command -v curl >/dev/null 2>&1; then
    echo "curl not found; the watchdog cannot probe $SHIM_HEALTH_URL"
    return 0
  fi
  mkdir -p "$LOG_DIR" 2>/dev/null || true
  now="$(date +%s)"
  if shim_probe; then
    # Healthy: clear the slate. Writing "0 0" every cycle is what makes a
    # strike from before a machine sleep irrelevant, not just expired.
    shim_write_strikes 0 0
    return 0
  fi
  strikes="$(shim_read_strikes)"
  recorded="${strikes%% *}"
  count="${strikes##* }"
  if [ "$recorded" -gt 0 ] && [ $((now - recorded)) -gt "$SHIM_STRIKE_TTL" ]; then
    count=0
  fi
  count=$((count + 1))
  shim_write_strikes "$now" "$count"
  echo "$(date '+%Y-%m-%d %H:%M:%S') health probe failed (strike $count/$SHIM_WATCHDOG_STRIKES): $SHIM_HEALTH_URL"
  if [ "$count" -lt "$SHIM_WATCHDOG_STRIKES" ]; then
    return 0
  fi
  echo "$(date '+%Y-%m-%d %H:%M:%S') $SHIM_WATCHDOG_STRIKES failed probes in a row; restarting $SHIM_LABEL"
  shim_restart
  shim_write_strikes 0 0
  return 0
}

install_watchdog_plist() {
  mkdir -p "$LAUNCH_DIR" "$LOG_DIR"
  # Task Scheduler rejects a repetition interval under one minute (platform.sh
  # floors it); report the cadence that actually gets registered, not the
  # configured one. The in-process watchdog keeps IMAGE_CAP_WATCHDOG_INTERVAL.
  local timer_interval="$SHIM_WATCHDOG_INTERVAL"
  if [ "$(fleet_os 2>/dev/null)" = "windows" ] && [ -n "$timer_interval" ] \
     && [ "$timer_interval" -lt 60 ] 2>/dev/null; then
    timer_interval=60
  fi
  fleet_timer_install "$SHIM_WATCHDOG_LABEL" "$timer_interval" \
    "$(command -v bash || echo /bin/bash)" "$SELF" "watchdog"
  echo "installed $SHIM_WATCHDOG_LABEL: probes $SHIM_HEALTH_URL every ${timer_interval}s, restarts after $SHIM_WATCHDOG_STRIKES failures  log $SHIM_WATCHDOG_LOG"
}

remove_watchdog_plist() {
  fleet_service_remove "$SHIM_WATCHDOG_LABEL" || true
  echo "removed $SHIM_WATCHDOG_LABEL"
}

install_shim_plist() {
  mkdir -p "$LAUNCH_DIR" "$LOG_DIR"
  fleet_service_install "$SHIM_LABEL" "$FLEET_HOME" "$ENVPAIRS" "$PY" "$SHIM_PY" ""
  echo "installed $SHIM_LABEL: $SHIM_BASE_URL -> $SHIM_UPSTREAM  (cc pin every ${SHIM_CC_PIN}s, codex repin every ${SHIM_REPIN}s)  log $SHIM_LOG"
}

remove_shim_plist() {
  fleet_service_remove "$SHIM_LABEL"
  echo "removed $SHIM_LABEL"
}

case "$CMD" in
  run)
    export $EXPORTS
    exec "$PY" "$SHIM_PY"
    ;;
  watchdog)
    watchdog_cycle
    ;;
  start)
    if pid="$(shim_pid)"; then
      echo "already running (pid $pid): $SHIM_BASE_URL"
      exit 0
    fi
    mkdir -p "$LOG_DIR"
    export $EXPORTS
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
    if fleet_service_exists "$SHIM_WATCHDOG_LABEL" 2>/dev/null; then
      watchdog_interval="$(awk '/<key>StartInterval<\/key>/{f=1;next} f&&/<integer>/{gsub(/<[^>]*>/,"");sub(/^ +/,"");sub(/ +$/,"");print;exit}' "$LAUNCH_DIR/${SHIM_WATCHDOG_LABEL}.plist" 2>/dev/null || true)"
      echo "watchdog: $SHIM_WATCHDOG_LABEL every ${watchdog_interval:-$SHIM_WATCHDOG_INTERVAL}s (restart after $SHIM_WATCHDOG_STRIKES failed probes)"
    else
      echo "watchdog: not installed (install it: bash $0 install-watchdog)"
    fi
    if [ -f "$SHIM_STRIKES_FILE" ]; then
      echo "watchdog strikes: $(cat "$SHIM_STRIKES_FILE" 2>/dev/null || echo '0 0') (epoch count)"
    fi
    if [ -f "$SHIM_WATCHDOG_LOG" ]; then
      echo "watchdog log (last 3 lines):"
      tail -3 "$SHIM_WATCHDOG_LOG" | sed 's/^/  /'
    fi
    ;;
  install-timer)
    # The watchdog goes in with the service: an install that only keeps the
    # process up cannot recover it from a hung loop, which is the failure this
    # pair of watchers exists for.
    install_shim_plist
    install_watchdog_plist
    ;;
  uninstall-timer)
    remove_shim_plist
    remove_watchdog_plist
    ;;
  install-watchdog)
    install_watchdog_plist
    ;;
  uninstall-watchdog)
    remove_watchdog_plist
    ;;
esac

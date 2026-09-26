#!/usr/bin/env bash
# FleetKit status: per-bridge health table + opencodex proxy status.
#
# Usage: status.sh [--home DIR] [--env-file PATH] [-h|--help]
set -euo pipefail

ARG_HOME=""
ARG_ENV=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --home)
      shift
      [ "$#" -gt 0 ] || { echo "--home requires a value" >&2; exit 1; }
      ARG_HOME="$1"
      ;;
    --home=*) ARG_HOME="$(echo "$1" | cut -d= -f2-)" ;;
    --env-file)
      shift
      [ "$#" -gt 0 ] || { echo "--env-file requires a value" >&2; exit 1; }
      ARG_ENV="$1"
      ;;
    --env-file=*) ARG_ENV="$(echo "$1" | cut -d= -f2-)" ;;
    -h|--help)
      echo "Usage: status.sh [--home DIR] [--env-file PATH]"
      exit 0
      ;;
    *)
      echo "unknown option: $1" >&2
      exit 1
      ;;
  esac
  shift
done

FLEET_HOME="$ARG_HOME"
if [ -z "$FLEET_HOME" ]; then FLEET_HOME="$HOME/FleetKit/runtime"; fi
ENVFILE="$ARG_ENV"
if [ -z "$ENVFILE" ]; then ENVFILE="$FLEET_HOME/fleet.env"; fi
if [ ! -f "$ENVFILE" ]; then
  echo "fleet.env not found: $ENVFILE (run install.sh first)" >&2
  exit 1
fi
set -a
. "$ENVFILE"
set +a

PORT_BASE="${PORT_BASE:-8787}"
LABEL_PREFIX="${LABEL_PREFIX:-com.local}"
LOG_DIR="${LOG_DIR:-/tmp/fleet-logs}"

md5short() {
  if command -v md5 >/dev/null 2>&1; then
    printf '%s' "$1" | md5 | awk '{print $NF}' | cut -c1-8
  else
    printf '%s' "$1" | md5sum | awk '{print $1}' | cut -c1-8
  fi
}

echo "FleetKit status (home $FLEET_HOME, ports $PORT_BASE..$((PORT_BASE + 8)))"
printf '%-14s %-6s %-7s %-7s %-10s %-16s %s
' BRIDGE PORT AGENT LISTEN MODELS KEY-MD5 LABEL
printf '%.0s-' $(seq 1 96)
echo

# name | label-suffix | port-offset | key-env
while IFS='|' read -r name labelsuffix offset keyenv; do
  [ -z "$name" ] && continue
  label="$LABEL_PREFIX.$labelsuffix"
  port=$((PORT_BASE + offset))

  if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
    agent=up
  else
    agent=DOWN
  fi
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    listen=yes
  else
    listen=no
  fi
  count="$(curl -s -m 5 "http://127.0.0.1:$port/v1/models" 2>/dev/null | grep -o '"id"' | wc -l | tr -d ' ' || true)"
  eval "key=\$$keyenv"
  kmd5="$(md5short "$key")"
  printf '%-14s %-6s %-7s %-7s %-10s %-16s %s
' "$name" "$port" "$agent" "$listen" "models=$count" "$kmd5" "$label"
done <<'ROWS'
workbuddy|workbuddy2codex|0|CODEBUDDY2OPENAI_KEY
workbuddy-gpt|workbuddy2codex-gpt|1|CODEBUDDY2OPENAI_KEY
qoder|qoder2codex|2|QODER2CODEX_KEY
codely|codely2codex|3|CODELY2CODEX_KEY
trae|trae2codex|4|TRAE2CODEX_KEY
lingxi|lingxi2codex|5|LINGXI2CODEX_KEY
xhx|xhx2codex|6|XHX2CODEX_KEY
gemini|gemini2codex|7|GEMINI2CODEX_KEY
catpaw|catpaw2codex|8|CATPAW2CODEX_KEY
ROWS

echo
if ls "$LOG_DIR"/*-bridge.log >/dev/null 2>&1; then
  echo "logs: $LOG_DIR/<bridge>-bridge.log"
fi
echo "hint: a bridge with models=0 usually needs a login, then:"
echo "      bash $FLEET_HOME/bridges/finish.sh <name>"
echo

if command -v ocx >/dev/null 2>&1; then
  echo "opencodex:"
  ocx status 2>&1 | sed 's/^/  /' | head -20
else
  echo "opencodex: not installed (npm install -g @bitkyc08/opencodex)"
fi

echo
echo "check-in:"
CHECKIN_STATE="$FLEET_HOME/checkin/state.json"
if [ -f "$CHECKIN_STATE" ]; then
  python3 - "$CHECKIN_STATE" "$(date +%Y-%m-%d)" <<'PY'
import json, sys
state = json.load(open(sys.argv[1]))
today = sys.argv[2]
if not state:
    print("  (no tasks recorded yet)")
for name in sorted(state):
    s = state[name]
    mark = "OK today" if s.get("last_success_date") == today else "not today"
    print("  %-6s %-10s | last %s | points %s | %s" % (
        name, mark, s.get("at", "never"), s.get("available_points", "-"),
        str(s.get("detail", ""))[:60]))
PY
else
  echo "  (no state yet; run: bash $FLEET_HOME/tools/checkin.sh --home $FLEET_HOME status)"
fi

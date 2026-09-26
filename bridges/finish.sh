#!/usr/bin/env bash
# FleetKit per-bridge finalizer.
#
# Restarts one bridge, waits for it to come up, lists its models, refreshes
# the Codex model catalog (when inject_catalog.py exists), syncs opencodex
# and runs a one-shot smoke chat.
#
# Usage: finish.sh <name> [--home DIR] [--tries N] [--skip-chat] [-h|--help]
#   names: workbuddy workbuddy-gpt qoder codely trae lingxi xhx gemini catpaw antigravity qwen
set -euo pipefail

NAME=""
ARG_HOME=""
TRIES=10
SKIP_CHAT=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    -h|--help)
      echo "Usage: finish.sh <name> [--home DIR] [--tries N] [--skip-chat]"
      exit 0
      ;;
    --home)
      shift
      [ "$#" -gt 0 ] || { echo "--home requires a value" >&2; exit 1; }
      ARG_HOME="$1"
      ;;
    --home=*) ARG_HOME="$(echo "$1" | cut -d= -f2-)" ;;
    --tries)
      shift
      [ "$#" -gt 0 ] || { echo "--tries requires a value" >&2; exit 1; }
      TRIES="$1"
      ;;
    --tries=*) TRIES="$(echo "$1" | cut -d= -f2-)" ;;
    --skip-chat) SKIP_CHAT=1 ;;
    -*)
      echo "unknown option: $1" >&2
      exit 1
      ;;
    *)
      if [ -z "$NAME" ]; then
        NAME="$1"
      else
        echo "unexpected argument: $1" >&2
        exit 1
      fi
      ;;
  esac
  shift
done

if [ -z "$NAME" ]; then
  echo "bridge name required" >&2
  echo "names: workbuddy workbuddy-gpt qoder codely trae lingxi xhx gemini catpaw antigravity qwen" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
FLEET_HOME="$ARG_HOME"
if [ -z "$FLEET_HOME" ]; then
  FLEET_HOME="$(cd "$SCRIPT_DIR/.." && pwd)"
fi
ENVFILE="$FLEET_HOME/fleet.env"
if [ ! -f "$ENVFILE" ]; then
  echo "fleet.env not found: $ENVFILE (run install.sh first)" >&2
  exit 1
fi
set -a
. "$ENVFILE"
set +a

PORT_BASE="${PORT_BASE:-8787}"
LABEL_PREFIX="${LABEL_PREFIX:-com.local}"
LAUNCH_DIR="${LAUNCH_DIR:-$HOME/Library/LaunchAgents}"
FLEET_PYTHON="${FLEET_PYTHON:-python3}"

# name | label-suffix | port-offset | bridge-dir | key-env | catalog-inject-prefix
found=""
suffix=""
offset=""
bridgedir=""
keyenv=""
inj=""
while IFS='|' read -r nm sf of dr ke ip; do
  [ -z "$nm" ] && continue
  if [ "$nm" = "$NAME" ]; then
    found=yes
    suffix="$sf"
    offset="$of"
    bridgedir="$dr"
    keyenv="$ke"
    inj="$ip"
    break
  fi
done <<'CASES'
workbuddy|workbuddy2codex|0|workbuddy2codex|CODEBUDDY2OPENAI_KEY|
workbuddy-gpt|workbuddy2codex-gpt|1|workbuddy2codex|CODEBUDDY2OPENAI_KEY|
qoder|qoder2codex|2|qoder|QODER2CODEX_KEY|QODER
codely|codely2codex|3|codely|CODELY2CODEX_KEY|CODELY
trae|trae2codex|4|trae|TRAE2CODEX_KEY|TRAE
lingxi|lingxi2codex|5|lingxi|LINGXI2CODEX_KEY|LINGXI
xhx|xhx2codex|6|xhx|XHX2CODEX_KEY|XHX
gemini|gemini2codex|7|gemini|GEMINI2CODEX_KEY|
catpaw|catpaw2codex|8|catpaw|CATPAW2CODEX_KEY|
antigravity|antigravity2codex|10|antigravity|ANTIGRAVITY2CODEX_KEY|antigravity_bridge.py
qwen|qwen2codex|11|qwen|QWEN2CODEX_KEY|QWEN
cline|cline2codex|12|cline|CLINE2CODEX_KEY|cline_bridge.py
CASES
if [ -z "$found" ]; then
  echo "unknown bridge: $NAME" >&2
  echo "names: workbuddy workbuddy-gpt qoder codely trae lingxi xhx gemini catpaw antigravity qwen" >&2
  exit 1
fi

port=$((PORT_BASE + offset))
label="$LABEL_PREFIX.$suffix"
service="gui/$(id -u)/$label"
eval "key=\$$keyenv"
PY="$FLEET_PYTHON"
if [ ! -x "$PY" ]; then PY=python3; fi

echo "== finish $NAME =="
echo "service : $service"
echo "port    : $port"
echo "bridge  : $FLEET_HOME/bridges/$bridgedir"

if ! launchctl print "$service" >/dev/null 2>&1; then
  echo "service is not loaded; run install.sh (or bootstrap $LAUNCH_DIR/$label.plist) first" >&2
  exit 1
fi

echo "[1/5] restarting service"
launchctl kickstart -k "$service"

echo "[2/5] waiting for /v1/models (up to $TRIES tries)"
count=0
i=0
while [ "$i" -lt "$TRIES" ]; do
  count="$(curl -s -m 5 "http://127.0.0.1:$port/v1/models" 2>/dev/null | grep -o '"id"' | wc -l | tr -d ' ' || true)"
  if [ "$count" -gt 0 ] 2>/dev/null; then
    break
  fi
  i=$((i + 1))
  sleep 2
done
if [ "$count" -gt 0 ] 2>/dev/null; then
  echo "  models=$count"
else
  echo "  no models yet (not logged in yet, or bridge still warming up)" >&2
  echo "  check the log and README 'logins' table, then re-run this script" >&2
  exit 3
fi

echo "[3/5] model list"
ids="$(curl -s -m 5 "http://127.0.0.1:$port/v1/models" | grep -o '"id"[[:space:]]*:[[:space:]]*"[^"]*"' | sed 's/.*"id"[[:space:]]*:[[:space:]]*"//;s/"$//' | head -8 || true)"
echo "$ids" | sed 's/^/  /'

echo "[4/5] Codex model catalog"
INJ="$SCRIPT_DIR/$bridgedir/inject_catalog.py"
CATALOG="$HOME/.codex/cc-switch-model-catalog.json"
if [ -n "$inj" ] && [ -f "$INJ" ]; then
  if [ -f "$CATALOG" ]; then
    varname="$inj"_BRIDGE
    eval "export $varname=http://127.0.0.1:$port"
    "$PY" "$INJ" || echo "  [warn] inject_catalog failed" >&2
  else
    echo "  skipped: $CATALOG does not exist on this machine"
  fi
else
  echo "  skipped: no inject_catalog.py for $NAME"
fi
if command -v ocx >/dev/null 2>&1; then
  echo "  ocx sync"
  ocx sync || echo "  [warn] ocx sync failed" >&2
else
  echo "  skipped: ocx not installed"
fi

echo "[5/5] smoke chat"
first="$(echo "$ids" | head -1)"
if [ "$SKIP_CHAT" = "1" ]; then
  echo "  skipped (--skip-chat)"
elif [ -z "$first" ]; then
  echo "  skipped: no model id"
else
  body="$(printf '{"model":"%s","messages":[{"role":"user","content":"请只回复两个字：正常"}],"max_tokens":16,"stream":false}' "$first")"
  curl -s -m 60 -X POST "http://127.0.0.1:$port/v1/chat/completions"     -H "Content-Type: application/json"     -H "Authorization: Bearer $key"     -d "$body" | head -c 600
  echo
fi

echo
echo "done: $NAME is ready. In Codex the models appear as $NAME/<model>."

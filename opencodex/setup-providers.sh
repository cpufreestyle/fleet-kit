#!/usr/bin/env bash
# Registers the fleet bridges with opencodex (ocx) so Codex can call them
# through http://127.0.0.1:10100/v1.
set -euo pipefail

ARG_HOME=""
ARG_ENV=""
DRY_RUN=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --home)
      shift
      [ "$#" -gt 0 ] || { echo "--home requires a value" >&2; exit 1; }
      ARG_HOME="$1"
      ;;
    --home=*) ARG_HOME="${1#*=}" ;;
    --env-file)
      shift
      [ "$#" -gt 0 ] || { echo "--env-file requires a value" >&2; exit 1; }
      ARG_ENV="$1"
      ;;
    --env-file=*) ARG_ENV="${1#*=}" ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help)
      echo "Usage: setup-providers.sh [--home DIR] [--env-file PATH] [--dry-run]"
      exit 0
      ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
  shift
done

if [ -n "$ARG_ENV" ]; then
  ENVFILE="$ARG_ENV"
else
  ENVFILE="${ARG_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}/fleet.env"
fi
if [ ! -f "$ENVFILE" ]; then
  echo "fleet.env not found: ${ENVFILE} (run install.sh first)" >&2
  exit 1
fi

set -a
. "$ENVFILE"
set +a

# an explicit --home wins over whatever fleet.env recorded
if [ -n "$ARG_HOME" ]; then
  FLEET_HOME="$ARG_HOME"
fi
PORT_BASE="${PORT_BASE:-8787}"
LABEL_PREFIX="${LABEL_PREFIX:-com.local}"

if ! command -v ocx >/dev/null 2>&1; then
  echo "ocx not found; install it with: npm install -g @bitkyc08/opencodex" >&2
  exit 1
fi

run() {
  if [ "$DRY_RUN" = "1" ]; then
    echo "  [dry-run] $*"
  else
    "$@" || echo "  [warn] command failed: $*" >&2
  fi
}

# name | port-offset | key-env
PROVIDERS=(
  "workbuddy|0|CODEBUDDY2OPENAI_KEY"
  "workbuddy-gpt|1|CODEBUDDY2OPENAI_KEY"
  "qoder|2|QODER2CODEX_KEY"
  "codely|3|CODELY2CODEX_KEY"
  "trae|4|TRAE2CODEX_KEY"
  "lingxi|5|LINGXI2CODEX_KEY"
  "xhx|6|XHX2CODEX_KEY"
  "gemini|7|GEMINI2CODEX_KEY"
  "catpaw|8|CATPAW2CODEX_KEY"
)

echo "opencodex provider setup (port base ${PORT_BASE})"
run ocx service

for row in "${PROVIDERS[@]}"; do
  name="$(echo "$row" | cut -d'|' -f1)"
  offset="$(echo "$row" | cut -d'|' -f2)"
  keyenv="$(echo "$row" | cut -d'|' -f3)"
  port=$((PORT_BASE + offset))
  key="${!keyenv:-}"
  if [ -z "$key" ]; then
    # live fleets keep keys in the launchd plists while fleet.env may be absent;
    # same source tools/fleet_chat_test.py reads.
    for pl in "$HOME/Library/LaunchAgents/${LABEL_PREFIX}.${name}2codex.plist" "$HOME/Library/LaunchAgents/${LABEL_PREFIX}.workbuddy2codex-gpt.plist"; do
      [ -f "$pl" ] || continue
      got="$(/usr/libexec/PlistBuddy -c "Print :EnvironmentVariables:${keyenv}" "$pl" 2>/dev/null || true)"
      if [ -n "$got" ]; then key="$got"; break; fi
    done
  fi
  [ -z "$key" ] && key="local"   # bridges that do not enforce a key tolerate this
  run ocx provider add "$name" --adapter openai-chat --base-url "http://127.0.0.1:${port}/v1" --api-key "$key" --allow-private-network --force
  run ocx models provider "$name" on
done

if [ -n "${TOKENDANCE_API_KEY:-}" ]; then
  run ocx provider add tokendance --adapter openai-chat --base-url "https://tokendance.space/gateway/v1" --api-key "${TOKENDANCE_API_KEY}" --allow-private-network --force
  run ocx models provider tokendance on
  run ocx models selected tokendance --set step-5-preview
else
  echo "  (TOKENDANCE_API_KEY not set; skipping tokendance)"
fi

run ocx sync
run ocx service restart
echo "done. inspect with: ocx models live"

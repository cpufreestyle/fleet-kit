#!/usr/bin/env bash
# Stops the fleet bridges, removes their service definitions (launchd plists on
# macOS, scheduled tasks on Windows, supervisor wrappers on Linux), and
# optionally deletes the fleet directory. opencodex providers are left as-is.
set -euo pipefail

ARG_HOME=""
PURGE=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --purge) PURGE=1 ;;
    --home)
      shift
      [ "$#" -gt 0 ] || { echo "--home requires a value" >&2; exit 1; }
      ARG_HOME="$1"
      ;;
    --home=*) ARG_HOME="${1#*=}" ;;
    -h|--help)
      echo "Usage: uninstall.sh [--home DIR] [--purge]"
      exit 0
      ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
  shift
done

ARG_HOME="${ARG_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "${SCRIPT_DIR}/tools/platform.sh" ]; then
  # shellcheck source=tools/platform.sh
  . "${SCRIPT_DIR}/tools/platform.sh"
fi
ENVFILE="${ARG_HOME}/fleet.env"
if [ -f "$ENVFILE" ]; then
  set -a
  . "$ENVFILE"
  set +a
fi
if command -v fleet_service_dir >/dev/null 2>&1; then
  LAUNCH_DIR="$(fleet_service_dir)"
fi
LAUNCH_DIR="${LAUNCH_DIR:-${HOME}/Library/LaunchAgents}"
FLEET_SERVICE_DIR="${FLEET_SERVICE_DIR:-${LAUNCH_DIR}}"
LABEL_PREFIX="${LABEL_PREFIX:-com.local}"
export FLEET_SERVICE_DIR

SUFFIXES="workbuddy2codex workbuddy2codex-gpt qoder2codex codely2codex trae2codex lingxi2codex xhx2codex gemini2codex catpaw2codex antigravity2codex qwen2codex cline2codex zcode2codex fleet-checkin fleet-ui ocx-catalog-guard fleet-probe"

for suffix in $SUFFIXES; do
  label="${LABEL_PREFIX}.${suffix}"
  # only touch services this fleet actually installed, so a wrong --home can
  # never stop another fleet
  if fleet_service_exists "$label"; then
    fleet_service_remove "$label"
    echo "removed ${label}"
  fi
done

echo "bridges stopped; services removed"
echo "opencodex providers were left as-is; remove them with: ocx provider remove <name>"

if [ "$PURGE" = "1" ]; then
  rm -rf "$ARG_HOME"
  echo "purged $ARG_HOME"
fi

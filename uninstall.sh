#!/usr/bin/env bash
# Stops the fleet bridges, removes their launchd plists, and optionally
# deletes the fleet directory. opencodex providers are left untouched.
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

ARG_HOME="${ARG_HOME:-${HOME}/fleet}"
ENVFILE="${ARG_HOME}/fleet.env"
if [ -f "$ENVFILE" ]; then
  set -a
  . "$ENVFILE"
  set +a
fi
LAUNCH_DIR="${LAUNCH_DIR:-${HOME}/Library/LaunchAgents}"
LABEL_PREFIX="${LABEL_PREFIX:-com.local}"

SUFFIXES="workbuddy2codex workbuddy2codex-gpt qoder2codex codely2codex trae2codex lingxi2codex xhx2codex gemini2codex catpaw2codex fleet-checkin fleet-ui"

for suffix in $SUFFIXES; do
  label="${LABEL_PREFIX}.${suffix}"
  # only touch services whose plist exists in the resolved LAUNCH_DIR,
  # so a wrong --home can never bootout another fleet
  if [ -f "${LAUNCH_DIR}/${label}.plist" ]; then
    launchctl bootout "gui/$(id -u)/${label}" >/dev/null 2>&1 || true
    rm -f "${LAUNCH_DIR}/${label}.plist"
    echo "removed ${label}"
  fi
done

echo "bridges stopped; plists removed"
echo "opencodex providers were left as-is; remove them with: ocx provider remove <name>"

if [ "$PURGE" = "1" ]; then
  rm -rf "$ARG_HOME"
  echo "purged $ARG_HOME"
fi

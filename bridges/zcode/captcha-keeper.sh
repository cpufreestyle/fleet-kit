#!/usr/bin/env bash
# FleetKit zcode captcha pool keeper control.
#
# The pool keeper (captcha-pool-keeper.sh) tops the captcha ticket pool up on
# a launchd timer, so a call never has to wait on a mint: the bridge and the
# CLI route spend from the pool, and only a pool that stays dry for a whole
# timer tick costs a mint -- which runs offscreen and invisible (see the
# keeper's header for why it is headed at all).
#
# Usage: captcha-keeper.sh <install|uninstall|run|status|kick> [--home DIR]
set -euo pipefail

CMD=""
ARG_HOME=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -h|--help)
      echo "Usage: captcha-keeper.sh <install|uninstall|run|status|kick> [--home DIR]"
      exit 0
      ;;
    --home)
      shift
      [ "$#" -gt 0 ] || { echo "--home requires a value" >&2; exit 1; }
      ARG_HOME="$1"
      ;;
    install|uninstall|run|status|kick)
      if [ -z "$CMD" ]; then CMD="$1"; else echo "unexpected argument: $1" >&2; exit 1; fi
      ;;
    *) echo "unexpected argument: $1" >&2; exit 1 ;;
  esac
  shift
done

if [ -z "$CMD" ]; then
  echo "command required: install | uninstall | run | status | kick" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
FLEET_HOME="$ARG_HOME"
if [ -z "$FLEET_HOME" ]; then
  # Walk up for the platform helpers: the checkout this script lives in,
  # which is also the tree the timer must then run -- a keeper installed
  # from kit would fill the kit pool no bridge ever reads.
  dir="$SCRIPT_DIR"
  while [ "$dir" != "/" ]; do
    if [ -f "$dir/tools/platform.sh" ]; then FLEET_HOME="$dir"; break; fi
    dir="$(dirname "$dir")"
  done
fi
set +u
if [ -f "$FLEET_HOME/tools/platform.sh" ]; then
  # shellcheck source=tools/platform.sh
  . "$FLEET_HOME/tools/platform.sh"
fi
if [ -z "$LABEL_PREFIX" ]; then LABEL_PREFIX="com.local"; fi
set -u

KEEPER_SH="$SCRIPT_DIR/captcha-pool-keeper.sh"
KEEPER_LABEL="${LABEL_PREFIX}.zcode-captcha-keeper"
KEEPER_INTERVAL="${ZCAP_KEEPER_INTERVAL:-120}"
SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"

case "$CMD" in
  install)
    if command -v fleet_timer_install >/dev/null 2>&1; then
      fleet_timer_install "$KEEPER_LABEL" "$KEEPER_INTERVAL" \
        "$(command -v bash || echo /bin/bash)" "$SELF" "run"
      echo "installed $KEEPER_LABEL: tops the pool up every ${KEEPER_INTERVAL}s (target ${ZCAP_KEEPER_TARGET:-2} tickets)"
    else
      echo "platform.sh not found; run $KEEPER_SH by hand or with cron" >&2
      exit 1
    fi
    ;;
  uninstall)
    if command -v fleet_service_remove >/dev/null 2>&1; then
      fleet_service_remove "$KEEPER_LABEL" || true
      echo "removed $KEEPER_LABEL"
    fi
    ;;
  run)
    bash "$KEEPER_SH"
    ;;
  kick)
    launchctl kickstart -k "gui/$(id -u)/$KEEPER_LABEL"
    ;;
  status)
    if command -v fleet_service_exists >/dev/null 2>&1 && \
       fleet_service_exists "$KEEPER_LABEL" 2>/dev/null; then
      echo "keeper: $KEEPER_LABEL every ${KEEPER_INTERVAL}s (target ${ZCAP_KEEPER_TARGET:-2} tickets)"
    else
      echo "keeper: not installed (install it: bash $SELF install)"
    fi
    read -r fresh stale pool < <(bash "$KEEPER_SH" --status)
    echo "pool: $pool ($fresh fresh, $stale stale)"
    ;;
esac

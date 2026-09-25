#!/usr/bin/env bash
# fleet-kit unattended deployment pipeline.
#
# preflight -> install bridges -> wait for ports -> finalize each bridge
# (finish.sh) -> opencodex providers -> optional check-in timer -> report.
# Missing logins are reported, not fatal. Safe to re-run.
#
# Usage: deploy.sh [--home DIR] [--port-base N] [--with-checkin]
#                  [--no-opencodex] [--smoke] [--update] [-h|--help]
set -euo pipefail

KIT_DIR="$(cd "$(dirname "$0")" && pwd)"
FLEET_HOME=""
PORT_BASE=""
WITH_OCX=1
WITH_CHECKIN=0
SMOKE=0
UPDATE=0
BRIDGES="workbuddy workbuddy-gpt qoder codely trae lingxi xhx gemini catpaw"

usage() {
  cat <<USAGE
fleet-kit deploy

Usage: deploy.sh [options]

  --home DIR        install root (default: ~/fleet)
  --port-base N     first bridge port; bridges use N..N+8 (default: 8787)
  --with-checkin    install the daily check-in timer (09:00 CST)
  --no-opencodex    skip opencodex provider wiring
  --smoke           run a chat smoke test per bridge (needs logins)
  --update          git pull the kit first (when run from a clone)
  -h, --help        show this help
USAGE
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --home)
      shift
      [ "$#" -gt 0 ] || { echo "--home requires a value" >&2; exit 1; }
      FLEET_HOME="$1"
      ;;
    --home=*) FLEET_HOME="$(echo "$1" | cut -d= -f2-)" ;;
    --port-base)
      shift
      [ "$#" -gt 0 ] || { echo "--port-base requires a value" >&2; exit 1; }
      PORT_BASE="$1"
      ;;
    --port-base=*) PORT_BASE="$(echo "$1" | cut -d= -f2-)" ;;
    --with-checkin) WITH_CHECKIN=1 ;;
    --no-opencodex) WITH_OCX=0 ;;
    --smoke) SMOKE=1 ;;
    --update) UPDATE=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 1 ;;
  esac
  shift
done

[ -n "$FLEET_HOME" ] || FLEET_HOME="$HOME/fleet"
[ -n "$PORT_BASE" ] || PORT_BASE=8787

port_of() {
  case "$1" in
    workbuddy) echo $((PORT_BASE + 0)) ;;
    workbuddy-gpt) echo $((PORT_BASE + 1)) ;;
    qoder) echo $((PORT_BASE + 2)) ;;
    codely) echo $((PORT_BASE + 3)) ;;
    trae) echo $((PORT_BASE + 4)) ;;
    lingxi) echo $((PORT_BASE + 5)) ;;
    xhx) echo $((PORT_BASE + 6)) ;;
    gemini) echo $((PORT_BASE + 7)) ;;
    catpaw) echo $((PORT_BASE + 8)) ;;
    *) echo "" ;;
  esac
}

echo "fleet-kit deploy"
echo "  kit   : $KIT_DIR"
echo "  home  : $FLEET_HOME"
echo "  ports : $PORT_BASE..$((PORT_BASE + 8))"

# [1/6] preflight
echo "[1/6] preflight"
if [ "$(uname -s)" != "Darwin" ]; then
  echo "macOS with launchd is required" >&2
  exit 1
fi
for tool in python3 curl launchctl; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "$tool is required but not found" >&2
    exit 1
  fi
done
BUSY=""
i=0
while [ "$i" -le 8 ]; do
  port=$((PORT_BASE + i))
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    BUSY="$BUSY $port"
  fi
  i=$((i + 1))
done
if [ -n "$BUSY" ]; then
  echo "ports already in use:$BUSY" >&2
  echo "if this is an older fleet, uninstall it first:" >&2
  echo "  bash <old-home>/uninstall.sh --home <old-home>" >&2
  exit 1
fi

# [2/6] kit update (optional)
if [ "$UPDATE" = "1" ]; then
  echo "[2/6] updating kit from git"
  if [ -d "$KIT_DIR/.git" ]; then
    git -C "$KIT_DIR" pull --ff-only || echo "[warn] git pull failed; using the local copy" >&2
  else
    echo "[warn] kit is not a git clone; --update skipped" >&2
  fi
else
  echo "[2/6] kit update skipped"
fi

# [3/6] install bridges
echo "[3/6] installing bridges"
INSTALL_ARGS="--home $FLEET_HOME --port-base $PORT_BASE"
if [ "$WITH_OCX" = "0" ]; then
  INSTALL_ARGS="$INSTALL_ARGS --no-opencodex"
fi
# shellcheck disable=SC2086
bash "$KIT_DIR/install.sh" $INSTALL_ARGS

# [4/6] wait for ports
echo "[4/6] waiting for bridges"
set -a
. "$FLEET_HOME/fleet.env"
set +a
pending="$BRIDGES"
deadline=$((SECONDS + 120))
while [ "$SECONDS" -lt "$deadline" ] && [ -n "$pending" ]; do
  still=""
  for name in $pending; do
    port="$(port_of "$name")"
    if curl -s -m 3 -o /dev/null "http://127.0.0.1:$port/v1/models" 2>/dev/null; then
      :
    else
      still="$still $name"
    fi
  done
  pending="$still"
  if [ -n "$pending" ]; then sleep 3; fi
done
# A bridge can stay down for reasons outside the kit (needs a VPN, geo-blocked,
# expired session, firewall). That must not abort the whole deploy: finalize the
# bridges that are up and report the rest so they can be fixed individually.
unreachable="$pending"
if [ -z "$unreachable" ]; then
  echo "all 9 bridges listening on $PORT_BASE..$((PORT_BASE + 8))"
else
  up=""
  for name in $BRIDGES; do
    case " $unreachable " in
      *" $name "*) ;;
      *) up="$up $name" ;;
    esac
  done
  up="${up# }"
  if [ -z "$up" ]; then
    echo "no bridges came up:$unreachable" >&2
    echo "check logs: bash $FLEET_HOME/tools/status.sh --home $FLEET_HOME" >&2
    exit 1
  fi
  echo "bridges up:$up (port range $PORT_BASE..$((PORT_BASE + 8)))"
  echo "[warn] unreachable (offline / needs VPN or login):$unreachable"
  echo "       inspect logs in $LOG_DIR, then: bash $FLEET_HOME/bridges/finish.sh <name>"
fi

# [5/6] finalize each bridge
echo "[5/6] finalizing bridges"
ok=0
failed=""
for name in $BRIDGES; do
  case " $unreachable " in
    *" $name "*) echo "  [skipped]      $name (bridge unreachable)"; continue ;;
  esac
  if bash "$FLEET_HOME/bridges/finish.sh" "$name" --home "$FLEET_HOME" --tries 5 --skip-chat >"$LOG_DIR/finish-$name.log" 2>&1; then
    ok=$((ok + 1))
    echo "  [ok]           $name"
  else
    rc=$?
    failed="$failed $name"
    echo "  [login needed] $name (exit $rc, log $LOG_DIR/finish-$name.log)"
  fi
done

# [6/6] wiring + report
echo "[6/6] wiring"
if [ "$WITH_OCX" = "1" ]; then
  if command -v ocx >/dev/null 2>&1; then
    bash "$FLEET_HOME/opencodex/setup-providers.sh" --env-file "$FLEET_HOME/fleet.env"
  else
    echo "[skip] ocx not found; wire providers later with:" >&2
    echo "       bash $FLEET_HOME/opencodex/setup-providers.sh --env-file $FLEET_HOME/fleet.env" >&2
  fi
fi
if [ "$WITH_CHECKIN" = "1" ]; then
  bash "$FLEET_HOME/tools/checkin.sh" --home "$FLEET_HOME" install-timer
fi

bash "$FLEET_HOME/tools/status.sh" --home "$FLEET_HOME" || true
echo
up_count=0
for name in $BRIDGES; do
  case " $unreachable " in
    *" $name "*) ;;
    *) up_count=$((up_count + 1)) ;;
  esac
done
echo "deploy summary: bridges $up_count/9 up | finalized $ok/$up_count | login needed:${failed:- none}"
if [ -n "$failed" ]; then
  echo "next: log in per README, then bash $FLEET_HOME/bridges/finish.sh <name>"
fi
echo "done."

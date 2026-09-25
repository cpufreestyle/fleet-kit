#!/usr/bin/env bash
# fleet-kit installer
#
# Deploys nine local reverse-proxy bridges as macOS launchd agents and
# optionally registers them with opencodex so Codex can call them.
#
# Bridges (default ports): workbuddy 8787, workbuddy-gpt 8788, qoder 8789,
# codely 8790, trae 8791, lingxi 8792, xhx 8793, gemini 8794, catpaw 8795.
set -euo pipefail

KIT_VERSION="1.0.0"
KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

FLEET_HOME="${HOME}/fleet"
PORT_BASE=8787
DRY_RUN=0
DO_START=1
WITH_OCX=1
SKIP_DEPS=0

usage() {
  cat <<'USAGE'
fleet-kit installer v1.0.0

Usage: install.sh [options]

  --home DIR        install root (default: ~/fleet)
  --port-base N     first bridge port; bridges use N..N+8 (default: 8787)
  --with-opencodex  register bridges with opencodex after install (default)
  --no-opencodex    skip opencodex wiring
  --no-start        write files and plists but do not launch bridges
  --skip-deps       do not create the virtualenv or install Python deps
  --dry-run         print the plan, change nothing
  -h, --help        show this help

Environment overrides (advanced: second fleet, CI):
  FLEET_LAUNCH_DIR   plist directory (default: ~/Library/LaunchAgents)
  FLEET_LABEL_PREFIX launchd label prefix (default: com.local)
  FLEET_LOG_DIR      bridge log directory (default: /tmp/fleet-logs)
USAGE
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --home)
      shift
      [ "$#" -gt 0 ] || { echo "--home requires a value" >&2; exit 1; }
      FLEET_HOME="$1"
      ;;
    --home=*) FLEET_HOME="${1#*=}" ;;
    --port-base)
      shift
      [ "$#" -gt 0 ] || { echo "--port-base requires a value" >&2; exit 1; }
      PORT_BASE="$1"
      ;;
    --port-base=*) PORT_BASE="${1#*=}" ;;
    --with-opencodex) WITH_OCX=1 ;;
    --no-opencodex) WITH_OCX=0 ;;
    --no-start) DO_START=0 ;;
    --skip-deps) SKIP_DEPS=1 ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 1 ;;
  esac
  shift
done

case "$FLEET_HOME" in
  *" "*) echo "FLEET_HOME must not contain spaces" >&2; exit 1 ;;
esac
case "$PORT_BASE" in
  ""|*[!0-9]*) echo "--port-base must be an integer" >&2; exit 1 ;;
esac

LAUNCH_DIR="${FLEET_LAUNCH_DIR:-${HOME}/Library/LaunchAgents}"
LABEL_PREFIX="${FLEET_LABEL_PREFIX:-com.local}"
LOG_DIR="${FLEET_LOG_DIR:-/tmp/fleet-logs}"

info() { echo "  $*"; }
run() {
  if [ "$DRY_RUN" = "1" ]; then
    echo "  [dry-run] $*"
  else
    "$@"
  fi
}

# name | label-suffix | bridge-dir | script | port-offset | key-env | extra-args | extra-env
BRIDGES=(
  "workbuddy|workbuddy2codex|workbuddy2codex|converter.py|0|CODEBUDDY2OPENAI_KEY|--host 127.0.0.1 --port @PORT@|"
  "workbuddy-gpt|workbuddy2codex-gpt|workbuddy2codex|converter.py|1|CODEBUDDY2OPENAI_KEY|--host 127.0.0.1 --port @PORT@ --auth-dir @FLEET_HOME@/auths-gpt|WORKBUDDY_AUTH_POOL_DIR=@FLEET_HOME@/auths-gpt"
  "qoder|qoder2codex|qoder|qoder_bridge.py|2|QODER2CODEX_KEY|--host 127.0.0.1 --port @PORT@|QODER_CALL_TIMEOUT=300"
  "codely|codely2codex|codely|codely_bridge.py|3|CODELY2CODEX_KEY|--host 127.0.0.1 --port @PORT@|CODELY_CALL_TIMEOUT=300"
  "trae|trae2codex|trae|trae_bridge.py|4|TRAE2CODEX_KEY|--host 127.0.0.1 --port @PORT@|TRAE_CALL_TIMEOUT=300"
  "lingxi|lingxi2codex|lingxi|lingxi_bridge.py|5|LINGXI2CODEX_KEY|--host 127.0.0.1 --port @PORT@|LINGXI_CALL_TIMEOUT=300"
  "xhx|xhx2codex|xhx|xhx_bridge.py|6|XHX2CODEX_KEY|--host 127.0.0.1 --port @PORT@|XHX_CALL_TIMEOUT=300"
  "gemini|gemini2codex|gemini|gemini_bridge.py|7|GEMINI2CODEX_KEY||GEMINI2CODEX_PORT=@PORT@"
  "catpaw|catpaw2codex|catpaw|catpaw_bridge.py|8|CATPAW2CODEX_KEY||CATPAW_PORT=@PORT@"
)

echo "fleet-kit installer v${KIT_VERSION}"
info "kit        : ${KIT_DIR}"
info "fleet home : ${FLEET_HOME}"
info "ports      : ${PORT_BASE} .. $((PORT_BASE + 8))"
info "launch dir : ${LAUNCH_DIR}"
info "log dir    : ${LOG_DIR}"
if [ "$DRY_RUN" = "1" ]; then info "mode       : DRY RUN (nothing is written)"; fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required but was not found in PATH" >&2
  exit 1
fi

# ---------- 1. layout ----------
run mkdir -p "${FLEET_HOME}/bridges"
run mkdir -p "${FLEET_HOME}/tools"
run mkdir -p "${FLEET_HOME}/docs"
run mkdir -p "${FLEET_HOME}/opencodex"
run mkdir -p "${FLEET_HOME}/auths-gpt"
run mkdir -p "$LOG_DIR"
run mkdir -p "$LAUNCH_DIR"

if [ "$DRY_RUN" = "1" ]; then
  info "would copy bridges/ tools/ docs/ opencodex/ README.md requirements.txt uninstall.sh"
else
  cp -R "${KIT_DIR}/bridges/." "${FLEET_HOME}/bridges/"
  cp -R "${KIT_DIR}/tools/." "${FLEET_HOME}/tools/"
  cp -R "${KIT_DIR}/docs/." "${FLEET_HOME}/docs/"
  cp -R "${KIT_DIR}/opencodex/." "${FLEET_HOME}/opencodex/"
  cp "${KIT_DIR}/README.md" "${FLEET_HOME}/README.md"
  cp "${KIT_DIR}/requirements.txt" "${FLEET_HOME}/requirements.txt"
  cp "${KIT_DIR}/uninstall.sh" "${FLEET_HOME}/uninstall.sh"
  chmod +x "${FLEET_HOME}/uninstall.sh"
fi

# ---------- 2. python environment ----------
FLEET_PYTHON="${FLEET_HOME}/.venv/bin/python"
if [ "$SKIP_DEPS" = "1" ]; then
  if [ ! -x "$FLEET_PYTHON" ]; then
    FLEET_PYTHON="$(command -v python3)"
  fi
  info "python     : ${FLEET_PYTHON} (deps skipped)"
elif [ -x "${FLEET_HOME}/.venv/bin/python" ] && [ -f "${FLEET_HOME}/.venv/.fleet-deps-ok" ]; then
  info "python     : ${FLEET_PYTHON} (reusing virtualenv)"
else
  echo "[2/5] creating virtualenv and installing dependencies"
  if [ "$DRY_RUN" = "1" ]; then
    info "[dry-run] python3 -m venv ${FLEET_HOME}/.venv ; pip install -r requirements.txt"
  else
    python3 -m venv "${FLEET_HOME}/.venv"
    "${FLEET_HOME}/.venv/bin/pip" install --quiet --upgrade pip
    "${FLEET_HOME}/.venv/bin/pip" install --quiet -r "${KIT_DIR}/requirements.txt"
    touch "${FLEET_HOME}/.venv/.fleet-deps-ok"
  fi
fi

# ---------- 3. fleet.env ----------
gen_key() {
  if command -v openssl >/dev/null 2>&1; then
    echo "sk-$(openssl rand -hex 24)"
  else
    python3 -c "import secrets; print('sk-' + secrets.token_hex(24))"
  fi
}

ENVFILE="${FLEET_HOME}/fleet.env"
EXISTING=""
if [ -f "$ENVFILE" ]; then
  EXISTING="$(cat "$ENVFILE")"
fi

pick_key() {
  local want="$1" value=""
  if [ "$DRY_RUN" = "1" ]; then
    echo "sk-dryrun"
    return 0
  fi
  if [ -n "$EXISTING" ]; then
    value="$(echo "$EXISTING" | grep -m1 "^${want}=" | cut -d= -f2- || true)"
  fi
  if [ -z "$value" ]; then
    case "$want" in
      GEMINI2CODEX_KEY) value="sk-local-gemini" ;;
      CATPAW2CODEX_KEY) value="sk-local-catpaw" ;;
      *) value="$(gen_key)" ;;
    esac
  fi
  echo "$value"
}

CODEBUDDY2OPENAI_KEY="$(pick_key CODEBUDDY2OPENAI_KEY)"
QODER2CODEX_KEY="$(pick_key QODER2CODEX_KEY)"
CODELY2CODEX_KEY="$(pick_key CODELY2CODEX_KEY)"
TRAE2CODEX_KEY="$(pick_key TRAE2CODEX_KEY)"
LINGXI2CODEX_KEY="$(pick_key LINGXI2CODEX_KEY)"
XHX2CODEX_KEY="$(pick_key XHX2CODEX_KEY)"
GEMINI2CODEX_KEY="$(pick_key GEMINI2CODEX_KEY)"
CATPAW2CODEX_KEY="$(pick_key CATPAW2CODEX_KEY)"

emit_fleet_env() {
  cat <<ENV
# fleet-kit environment -- generated by install.sh v${KIT_VERSION}
# Keep this file private: it holds the bridge API keys.
FLEET_HOME=${FLEET_HOME}
PORT_BASE=${PORT_BASE}
LAUNCH_DIR=${LAUNCH_DIR}
LABEL_PREFIX=${LABEL_PREFIX}
LOG_DIR=${LOG_DIR}
FLEET_PYTHON=${FLEET_PYTHON}

CODEBUDDY2OPENAI_KEY=${CODEBUDDY2OPENAI_KEY}
QODER2CODEX_KEY=${QODER2CODEX_KEY}
CODELY2CODEX_KEY=${CODELY2CODEX_KEY}
TRAE2CODEX_KEY=${TRAE2CODEX_KEY}
LINGXI2CODEX_KEY=${LINGXI2CODEX_KEY}
XHX2CODEX_KEY=${XHX2CODEX_KEY}
GEMINI2CODEX_KEY=${GEMINI2CODEX_KEY}
CATPAW2CODEX_KEY=${CATPAW2CODEX_KEY}

# Optional: TokenDance gateway models (https://tokendance.space)
# TOKENDANCE_API_KEY=
ENV
}

if [ "$DRY_RUN" = "1" ]; then
  info "[dry-run] would write ${ENVFILE} (mode 600) with 8 bridge keys"
else
  ( umask 077; emit_fleet_env > "$ENVFILE" )
fi

# ---------- 4. launchd agents ----------
detect_path() {
  local value="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
  local nodebin=""
  nodebin="$(command -v node || true)"
  if [ -n "$nodebin" ]; then
    value="$(dirname "$nodebin"):${value}"
  fi
  if [ -d /opt/homebrew/bin ]; then
    value="/opt/homebrew/bin:${value}"
  fi
  echo "$value"
}
PATH_VALUE="$(detect_path)"

plist_xml() {
  local name="$1" labelsuffix="$2" keyenv="$3" script="$4" workdir="$5" extra="$6" extraenv="$7"
  local label="${LABEL_PREFIX}.${labelsuffix}"
  local keyval="${!keyenv}"
  local entry k v arg
  local env_xml=""
  local args_xml=""
  if [ -n "$extraenv" ]; then
    local oifs="$IFS"
    IFS=';'
    for entry in $extraenv; do
      k="${entry%%=*}"
      v="${entry#*=}"
      env_xml="${env_xml}    <key>${k}</key>
    <string>${v}</string>
"
    done
    IFS="$oifs"
  fi
  if [ -n "$extra" ]; then
    local oifs="$IFS"
    IFS=' '
    for arg in $extra; do
      args_xml="${args_xml}    <string>${arg}</string>
"
    done
    IFS="$oifs"
  fi
  cat <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key>
    <string>${HOME}</string>
    <key>PATH</key>
    <string>${PATH_VALUE}</string>
    <key>${keyenv}</key>
    <string>${keyval}</string>
${env_xml}  </dict>
  <key>KeepAlive</key>
  <true/>
  <key>Label</key>
  <string>${label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${FLEET_PYTHON}</string>
    <string>${script}</string>
${args_xml}  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/${name}-bridge.log</string>
  <key>StandardOutPath</key>
  <string>${LOG_DIR}/${name}-bridge.log</string>
  <key>WorkingDirectory</key>
  <string>${workdir}</string>
</dict>
</plist>
PLIST
}

echo "[4/5] writing launchd agents"
for row in "${BRIDGES[@]}"; do
  name="$(echo "$row" | cut -d'|' -f1)"
  labelsuffix="$(echo "$row" | cut -d'|' -f2)"
  bridgedir="$(echo "$row" | cut -d'|' -f3)"
  script="$(echo "$row" | cut -d'|' -f4)"
  offset="$(echo "$row" | cut -d'|' -f5)"
  keyenv="$(echo "$row" | cut -d'|' -f6)"
  extra="$(echo "$row" | cut -d'|' -f7)"
  extraenv="$(echo "$row" | cut -d'|' -f8)"
  port=$((PORT_BASE + offset))
  extra="${extra//@PORT@/$port}"
  extra="${extra//@FLEET_HOME@/$FLEET_HOME}"
  extraenv="${extraenv//@PORT@/$port}"
  extraenv="${extraenv//@FLEET_HOME@/$FLEET_HOME}"
  label="${LABEL_PREFIX}.${labelsuffix}"
  plist="${LAUNCH_DIR}/${label}.plist"
  scriptpath="${FLEET_HOME}/bridges/${bridgedir}/${script}"
  workdir="${FLEET_HOME}/bridges/${bridgedir}"
  if [ ! -f "$scriptpath" ] && [ "$DRY_RUN" != "1" ]; then
    echo "  [warn] missing ${scriptpath}; skipping ${label}" >&2
    continue
  fi
  if [ "$DRY_RUN" = "1" ]; then
    info "[dry-run] ${label} -> :${port} (${plist})"
  else
    plist_xml "$name" "$labelsuffix" "$keyenv" "$scriptpath" "$workdir" "$extra" "$extraenv" > "$plist"
    if [ "$DO_START" = "1" ]; then
      if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
        echo "  [warn] port ${port} is already in use; ${label} may fail to bind" >&2
      fi
      launchctl bootout "gui/$(id -u)/${label}" >/dev/null 2>&1 || true
      launchctl bootstrap "gui/$(id -u)" "$plist"
      launchctl kickstart -k "gui/$(id -u)/${label}" >/dev/null 2>&1 || true
      info "started ${label} on :${port}"
    else
      info "wrote ${plist} (not started)"
    fi
  fi
done

# ---------- 5. opencodex ----------
if [ "$WITH_OCX" = "1" ]; then
  echo "[5/5] opencodex wiring"
  if ! command -v ocx >/dev/null 2>&1; then
    if [ "$DRY_RUN" = "1" ]; then
      info "[dry-run] npm install -g @bitkyc08/opencodex"
    else
      npm install -g @bitkyc08/opencodex || echo "  [warn] npm install failed; rerun opencodex/setup-providers.sh manually" >&2
    fi
  fi
  if [ "$DRY_RUN" = "1" ]; then
    TMPENV="$(mktemp -t fleet-dryrun)"
    emit_fleet_env > "$TMPENV"
    bash "${KIT_DIR}/opencodex/setup-providers.sh" --dry-run --env-file="$TMPENV" || true
    rm -f "$TMPENV"
  elif command -v ocx >/dev/null 2>&1; then
    bash "${FLEET_HOME}/opencodex/setup-providers.sh" || true
  else
    echo "  ocx unavailable; register providers later: bash ${FLEET_HOME}/opencodex/setup-providers.sh" >&2
  fi
fi

if [ "$DRY_RUN" = "1" ]; then
  echo
  echo "dry run complete; nothing was written."
  exit 0
fi

cat <<DONE

fleet-kit v${KIT_VERSION} installed at ${FLEET_HOME}

Next steps
  1. Log in to each service you want (README.md, "logins" table).
  2. After each login:  bash ${FLEET_HOME}/bridges/finish.sh <name>
  3. Health check:      bash ${FLEET_HOME}/tools/status.sh
  4. Full chat test:    python3 ${FLEET_HOME}/tools/fleet_chat_test.py

Codex usage: opencodex proxies the bridges at http://127.0.0.1:10100/v1.
Models appear as <bridge>/<model>, e.g. workbuddy/hy4-preview.
DONE

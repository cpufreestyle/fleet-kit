#!/usr/bin/env bash
# FleetKit installer
#
# Deploys eleven local reverse-proxy bridges as background services
# (macOS launchd agents, Windows Task Scheduler tasks, or a detached
# supervisor on Linux) and optionally registers them with opencodex so
# Codex can call them.
#
# Bridges (default ports): workbuddy 8787, workbuddy-gpt 8788, qoder 8789,
# codely 8790, trae 8791, lingxi 8792, xhx 8793, gemini 8794, catpaw 8795,
# qwen 8798 (Qwen Cloud 海外托管; qwen3.8-flash = Qwen4 架构生产版),
# zcode 8800 (Z.AI Coding Plan; GLM-5.3 / GLM-5.3-Flash 免费额度).
# antigravity 8797 (Cloudflare-style gap: 8796 is the status panel).
set -euo pipefail

KIT_VERSION="1.3.0"
KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Platform abstraction: launchd on macOS, Task Scheduler on Windows,
# a detached respawn wrapper on Linux. Everything below calls fleet_*.
if [ -f "${KIT_DIR}/tools/platform.sh" ]; then
  # shellcheck source=tools/platform.sh
  . "${KIT_DIR}/tools/platform.sh"
fi

FLEET_HOME="${HOME}/FleetKit/runtime"
PORT_BASE=8787
DRY_RUN=0
DO_START=1
WITH_OCX=1
SKIP_DEPS=0
WITH_CHECKIN=0
WITH_UI=0
WITH_OCX_GUARD=1
WITH_REACH=1

usage() {
  cat <<'USAGE'
FleetKit installer v1.1.0

Usage: install.sh [options]

  --home DIR        install root (default: ~/FleetKit/runtime)
  --port-base N     first bridge port; bridges use N..N+13 (default: 8787)
  --with-opencodex  register bridges with opencodex after install (default)
  --no-opencodex    skip opencodex wiring
  --with-checkin   install the daily check-in timer (09:00 CST)
  --with-ui        install the local status panel (port PORT_BASE+9)
  --no-ocx-guard   skip the ocx catalog guard timer (on when opencodex is wired)
  --no-start        write files and plists but do not launch bridges
  --skip-deps       do not create the virtualenv or install Python deps
  --dry-run         print the plan, change nothing
  -h, --help        show this help

Environment overrides (advanced: second fleet, CI):
  FLEET_SERVICE_DIR  service directory (macOS ~/Library/LaunchAgents,
                     Windows %LOCALAPPDATA%\FleetKit\services)
  FLEET_LAUNCH_DIR   alias of FLEET_SERVICE_DIR (kept for older scripts)
  FLEET_LABEL_PREFIX service label prefix (default: com.local)
  FLEET_LOG_DIR      bridge log directory (default: /tmp/fleet-logs)
  FLEET_OS           force a backend: macos | windows | linux
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
    --with-checkin) WITH_CHECKIN=1 ;;
    --with-ui) WITH_UI=1 ;;
    --no-ocx-guard) WITH_OCX_GUARD=0 ;;
    --no-reach) WITH_REACH=0 ;;
    --no-start) DO_START=0 ;;
    --skip-deps) SKIP_DEPS=1 ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 1 ;;
  esac
  shift
done

case "$FLEET_HOME" in
  *" "*) echo "  [warn] FLEET_HOME contains a space: $FLEET_HOME" >&2 ;;
esac
case "$PORT_BASE" in
  ""|*[!0-9]*) echo "--port-base must be an integer" >&2; exit 1 ;;
esac

if command -v fleet_service_dir >/dev/null 2>&1; then
  LAUNCH_DIR="$(fleet_service_dir)"
  LOG_DIR="$(fleet_log_dir)"
else
  LAUNCH_DIR="${FLEET_LAUNCH_DIR:-${HOME}/Library/LaunchAgents}"
  LOG_DIR="${FLEET_LOG_DIR:-/tmp/fleet-logs}"
fi
LAUNCH_DIR="${FLEET_LAUNCH_DIR:-${LAUNCH_DIR}}"
FLEET_SERVICE_DIR="${FLEET_SERVICE_DIR:-${LAUNCH_DIR}}"
LABEL_PREFIX="${FLEET_LABEL_PREFIX:-com.local}"
export FLEET_SERVICE_DIR FLEET_LOG_DIR="$LOG_DIR"

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
  "workbuddy|workbuddy2codex|workbuddy-cn|converter.py|0|CODEBUDDY2OPENAI_KEY|--host 127.0.0.1 --port @PORT@|"
  # no --auth-dir here on purpose: extra-args is word-split, so a path with
  # a space in FLEET_HOME becomes two argv entries and the bridge dies with
  # "unrecognized arguments". WORKBUDDY_AUTH_POOL_DIR names the same folder.
  "workbuddy-gpt|workbuddy2codex-gpt|workbuddy-gpt|converter.py|1|CODEBUDDY2OPENAI_KEY|--host 127.0.0.1 --port @PORT@|WORKBUDDY_AUTH_POOL_DIR=@FLEET_HOME@/bridges/workbuddy-gpt/auths;WORKBUDDY_LOCAL_STORAGE=@HOME@/.workbuddy-ai/local_storage"
  "qoder|qoder2codex|qoder|qoder_bridge.py|2|QODER2CODEX_KEY|--host 127.0.0.1 --port @PORT@|QODER_CALL_TIMEOUT=300"
  "codely|codely2codex|codely|codely_bridge.py|3|CODELY2CODEX_KEY|--host 127.0.0.1 --port @PORT@|CODELY_CALL_TIMEOUT=300"
  "trae|trae2codex|trae|trae_bridge.py|4|TRAE2CODEX_KEY|--host 127.0.0.1 --port @PORT@|TRAE_CALL_TIMEOUT=300"
  "lingxi|lingxi2codex|lingxi|lingxi_bridge.py|5|LINGXI2CODEX_KEY|--host 127.0.0.1 --port @PORT@|LINGXI_CALL_TIMEOUT=300"
  "xhx|xhx2codex|xhx|xhx_bridge.py|6|XHX2CODEX_KEY|--host 127.0.0.1 --port @PORT@|XHX_CALL_TIMEOUT=300"
  "gemini|gemini2codex|gemini|gemini_bridge.py|7|GEMINI2CODEX_KEY||GEMINI2CODEX_PORT=@PORT@"
  "catpaw|catpaw2codex|catpaw|catpaw_bridge.py|8|CATPAW2CODEX_KEY||CATPAW_PORT=@PORT@"
  "antigravity|antigravity2codex|antigravity|antigravity_bridge.py|10|ANTIGRAVITY2CODEX_KEY||ANTIGRAVITY2CODEX_PORT=@PORT@"
  "qwen|qwen2codex|qwen|qwen_bridge.py|11|QWEN2CODEX_KEY|--host 127.0.0.1 --port @PORT@|QWEN_CALL_TIMEOUT=300"
  "cline|cline2codex|cline|cline_bridge.py|12|CLINE2CODEX_KEY|--host 127.0.0.1 --port @PORT@|CLINE_CALL_TIMEOUT=300"
  "zcode|zcode2codex|zcode|zcode_bridge.py|13|ZCODE2CODEX_KEY|--host 127.0.0.1 --port @PORT@|ZCODE_CALL_TIMEOUT=300"
)

echo "FleetKit installer v${KIT_VERSION}"
info "kit        : ${KIT_DIR}"
info "fleet home : ${FLEET_HOME}"
info "ports      : ${PORT_BASE} .. $((PORT_BASE + 13))"
info "launch dir : ${LAUNCH_DIR}"
info "log dir    : ${LOG_DIR}"
if [ "$DRY_RUN" = "1" ]; then info "mode       : DRY RUN (nothing is written)"; fi

SYS_PYTHON="$(fleet_system_python 2>/dev/null || echo python3)"
if [ "$SYS_PYTHON" = "py" ]; then
  SYS_PYTHON="$(command -v py) -3"
fi
if ! command -v ${SYS_PYTHON%% *} >/dev/null 2>&1; then
  echo "python3 is required but was not found in PATH" >&2
  exit 1
fi

# Copy SRCDIR/. into DESTDIR. Destination symlinks are operator overrides
# (e.g. runtime/bridges/workbuddy-*/assets -> the npm module): keep them,
# because cp -R refuses to merge a real directory into a symlink.
copy_tree() {
  local src="$1" dst="$2" item rel target
  [ -d "$src" ] || return 0
  mkdir -p "$dst"
  while IFS= read -r item; do
    rel="${item#"$src"/}"
    target="$dst/$rel"
    if [ -L "$target" ]; then
      info "keeping symlink $target -> $(readlink "$target")"
    elif [ -d "$item" ] && [ ! -L "$item" ]; then
      if [ ! -d "$target" ]; then
        cp -R "$item" "$target" || info "warn: cannot copy $item"
      fi
    elif [ -d "$target" ]; then
      cp -R "$item" "$target/" || info "warn: cannot copy $item"
    else
      cp -R "$item" "$target" || info "warn: cannot copy $item"
    fi
  done < <(find "$src" -mindepth 1 | sort)
}

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
  copy_tree "${KIT_DIR}/bridges" "${FLEET_HOME}/bridges"
  copy_tree "${KIT_DIR}/tools" "${FLEET_HOME}/tools"
  copy_tree "${KIT_DIR}/docs" "${FLEET_HOME}/docs"
  copy_tree "${KIT_DIR}/opencodex" "${FLEET_HOME}/opencodex"
  cp "${KIT_DIR}/README.md" "${FLEET_HOME}/README.md"
  cp "${KIT_DIR}/requirements.txt" "${FLEET_HOME}/requirements.txt"
  cp "${KIT_DIR}/uninstall.sh" "${FLEET_HOME}/uninstall.sh"
  chmod +x "${FLEET_HOME}/uninstall.sh"
fi

# ---------- 2. python environment ----------
# venv layout: posix uses .venv/bin/python, Windows .venv/Scripts/python.exe
if command -v fleet_venv_python >/dev/null 2>&1; then
  FLEET_PYTHON="$(fleet_venv_python "${FLEET_HOME}")"
else
  FLEET_PYTHON="${FLEET_HOME}/.venv/bin/python"
fi
VENV_PIP="${FLEET_HOME}/.venv/bin/pip"
if command -v fleet_is_windows >/dev/null 2>&1 && fleet_is_windows; then
  VENV_PIP="${FLEET_HOME}/.venv/Scripts/pip.exe"
fi
if [ "$SKIP_DEPS" = "1" ]; then
  if [ ! -x "$FLEET_PYTHON" ]; then
    FLEET_PYTHON="$(command -v ${SYS_PYTHON%% *})"
  fi
  info "python     : ${FLEET_PYTHON} (deps skipped)"
elif [ -x "$FLEET_PYTHON" ] && [ -f "${FLEET_HOME}/.venv/.fleet-deps-ok" ]; then
  info "python     : ${FLEET_PYTHON} (reusing virtualenv)"
else
  echo "[2/5] creating virtualenv and installing dependencies"
  if [ "$DRY_RUN" = "1" ]; then
    info "[dry-run] ${SYS_PYTHON} -m venv ${FLEET_HOME}/.venv ; pip install -r requirements.txt"
  else
    ${SYS_PYTHON} -m venv "${FLEET_HOME}/.venv"
    "$VENV_PIP" install --quiet --upgrade pip
    "$VENV_PIP" install --quiet -r "${KIT_DIR}/requirements.txt"
    touch "${FLEET_HOME}/.venv/.fleet-deps-ok"
  fi
fi

# Several bridges are vendored from npm modules that ship their own pip venv
# containing fastapi. When FLEET_PYTHON cannot import fastapi the bridge dies at
# import time, so resolve a venv-capable fallback instead of crash-looping.
python_has_fastapi() { "$1" -c 'import fastapi' >/dev/null 2>&1; }

detect_venv_python() {
  local cand
  if [ -n "${FLEET_VENV_PYTHON:-}" ] && python_has_fastapi "${FLEET_VENV_PYTHON}"; then
    echo "${FLEET_VENV_PYTHON}"; return 0
  fi
  local venv_sub="bin/python"
  if command -v fleet_is_windows >/dev/null 2>&1 && fleet_is_windows; then
    venv_sub="Scripts/python.exe"
  fi
  for cand in "${HOME}"/.local/node-*/lib/node_modules/*/.venv/${venv_sub} \
              "${HOME}"/.local/share/pnpm/global/*/node_modules/*/.venv/${venv_sub} \
              "${FLEET_HOME}"/bridges/*/.venv/${venv_sub} \
              "${FLEET_HOME}"/bridges/*/.venv/bin/python; do
    [ -x "$cand" ] || continue
    if python_has_fastapi "$cand"; then echo "$cand"; return 0; fi
  done
  echo ""
}

FLEET_VENV_PYTHON="$(detect_venv_python)"
if [ -z "$FLEET_VENV_PYTHON" ]; then
  FLEET_VENV_PYTHON="${FLEET_PYTHON}"
fi
info "venv python: ${FLEET_VENV_PYTHON} (fastapi fallback)"

bridge_python() {
  if python_has_fastapi "${FLEET_PYTHON}"; then
    echo "${FLEET_PYTHON}"
  else
    echo "${FLEET_VENV_PYTHON}"
  fi
}

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
    value="$(echo "$EXISTING" | grep -m1 "^${want}=" | cut -d= -f2- | tr -d '"' || true)"
  fi
  if [ -z "$value" ]; then
    case "$want" in
      GEMINI2CODEX_KEY) value="sk-local-gemini" ;;
      CATPAW2CODEX_KEY) value="sk-local-catpaw" ;;
      ANTIGRAVITY2CODEX_KEY) value="sk-local-antigravity" ;;
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
ANTIGRAVITY2CODEX_KEY="$(pick_key ANTIGRAVITY2CODEX_KEY)"
QWEN2CODEX_KEY="$(pick_key QWEN2CODEX_KEY)"
CLINE2CODEX_KEY="$(pick_key CLINE2CODEX_KEY)"
ZCODE2CODEX_KEY="$(pick_key ZCODE2CODEX_KEY)"

# Antigravity google oauth client pair is never committed to git (push protection
# rejects it) and every install ships it in its own binary, so read it from there.
pick_agy_oauth() {
  local want="$1" value=""
  if [ -n "$EXISTING" ]; then
    value="$(echo "$EXISTING" | grep -m1 "^${want}=" | cut -d= -f2- | tr -d '"' || true)"
  fi
  echo "$value"
}

ANTIGRAVITY_OAUTH_CLIENT_ID="$(pick_agy_oauth ANTIGRAVITY_OAUTH_CLIENT_ID)"
ANTIGRAVITY_OAUTH_CLIENT_SECRET="$(pick_agy_oauth ANTIGRAVITY_OAUTH_CLIENT_SECRET)"
ANTIGRAVITY_LEGACY_CLIENTS="$(pick_agy_oauth ANTIGRAVITY_LEGACY_CLIENTS)"

if [ -z "$ANTIGRAVITY_OAUTH_CLIENT_ID" ] || [ -z "$ANTIGRAVITY_OAUTH_CLIENT_SECRET" ]; then
  AGY_BIN="${AGY_BIN:-/Applications/Antigravity.app/Contents/Resources/bin/language_server}"
  AGY_LINES="$(python3 "${KIT_DIR}/bridges/antigravity/extract_client.py" --verify "$AGY_BIN" 2>/dev/null || true)"
  if [ -z "$AGY_LINES" ]; then
    AGY_LINES="$(python3 "${KIT_DIR}/bridges/antigravity/extract_client.py" "$AGY_BIN" 2>/dev/null || true)"
  fi
  if [ -n "$AGY_LINES" ]; then
    ANTIGRAVITY_OAUTH_CLIENT_ID="$(printf '%s\n' "$AGY_LINES" | head -1 | cut -f1)"
    ANTIGRAVITY_OAUTH_CLIENT_SECRET="$(printf '%s\n' "$AGY_LINES" | head -1 | cut -f2)"
    ANTIGRAVITY_LEGACY_CLIENTS="$(printf '%s\n' "$AGY_LINES" | tail -n +2 | paste -sd, -)"
    info "antigravity oauth: pair read from $(basename "$AGY_BIN")"
  else
    echo "  [warn] no antigravity oauth client in ${AGY_BIN}; the bridge starts but" >&2
    echo "         refresh returns 401 until fleet.env sets ANTIGRAVITY_OAUTH_CLIENT_ID" >&2
    echo "         and ANTIGRAVITY_OAUTH_CLIENT_SECRET (bridges/antigravity/extract_client.py)" >&2
  fi
fi

emit_fleet_env() {
  local preserved=""
  if [ -n "$EXISTING" ]; then
    # Operator-owned keys are not managed by install.sh: carry the exact
    # previous lines across so a re-install never drops them.
    preserved="$(echo "$EXISTING" | grep -E '^(HOMEBREW_PYTHON|TOKENDANCE_API_KEY|STEPFUN_PLAN_API_KEY)=' || true)"
  fi
  cat <<ENV
# FleetKit environment -- generated by install.sh v${KIT_VERSION}
# Keep this file private: it holds the bridge API keys.
FLEET_HOME="${FLEET_HOME}"
PORT_BASE="${PORT_BASE}"
LAUNCH_DIR="${LAUNCH_DIR}"
LABEL_PREFIX="${LABEL_PREFIX}"
LOG_DIR="${LOG_DIR}"
FLEET_PYTHON="${FLEET_PYTHON}"
CODEX_CHECKIN_HOME="${FLEET_HOME}/checkin"

CODEBUDDY2OPENAI_KEY="${CODEBUDDY2OPENAI_KEY}"
QODER2CODEX_KEY="${QODER2CODEX_KEY}"
CODELY2CODEX_KEY="${CODELY2CODEX_KEY}"
TRAE2CODEX_KEY="${TRAE2CODEX_KEY}"
LINGXI2CODEX_KEY="${LINGXI2CODEX_KEY}"
XHX2CODEX_KEY="${XHX2CODEX_KEY}"
GEMINI2CODEX_KEY="${GEMINI2CODEX_KEY}"
CATPAW2CODEX_KEY="${CATPAW2CODEX_KEY}"
ANTIGRAVITY2CODEX_KEY="${ANTIGRAVITY2CODEX_KEY}"
QWEN2CODEX_KEY="${QWEN2CODEX_KEY}"
CLINE2CODEX_KEY="${CLINE2CODEX_KEY}"
ZCODE2CODEX_KEY="${ZCODE2CODEX_KEY}"
ANTIGRAVITY_OAUTH_CLIENT_ID="${ANTIGRAVITY_OAUTH_CLIENT_ID}"
ANTIGRAVITY_OAUTH_CLIENT_SECRET="${ANTIGRAVITY_OAUTH_CLIENT_SECRET}"
# Optional extra id:secret pairs tried after the primary (Antigravity rotates these).
ANTIGRAVITY_LEGACY_CLIENTS="${ANTIGRAVITY_LEGACY_CLIENTS}"

# Optional: TokenDance gateway models (https://tokendance.space).
# Operator keys preserved from the previous fleet.env are appended below.
${preserved}
ENV
}

if [ "$DRY_RUN" = "1" ]; then
  info "[dry-run] would write ${ENVFILE} (mode 600) with 11 bridge keys"
else
  ( umask 077; emit_fleet_env > "$ENVFILE" )
fi

# ---------- 4. background services (launchd / Task Scheduler / supervisor) ----------
# The service spec is platform-neutral: a label, a working directory, a list of
# KEY=VALUE pairs, an interpreter, a script and its extra args. tools/platform.sh
# turns that into a launchd plist, a scheduled task, or a respawning wrapper.
service_env() {
  local keyenv="$1" keyval="$2" extraenv="$3"
  local entry k v out=""
  if [ -n "$extraenv" ]; then
    local oifs="$IFS"
    IFS=';'
    for entry in $extraenv; do
      [ -n "$entry" ] || continue
      k="${entry%%=*}"
      v="${entry#*=}"
      [ -n "$k" ] || continue
      out="${out}${k}=${v};"
    done
    IFS="$oifs"
  fi
  printf '%s' "HOME=${HOME};PATH=$(fleet_detect_path);${keyenv}=${keyval};${out}"
}

echo "[4/5] installing bridge services ($(fleet_os 2>/dev/null || echo macos) backend)"
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
  extra="${extra//@HOME@/$HOME}"
  extraenv="${extraenv//@HOME@/$HOME}"
  if [ "$name" = "antigravity" ] && [ -n "$ANTIGRAVITY_OAUTH_CLIENT_ID" ]; then
    extraenv="${extraenv};ANTIGRAVITY_OAUTH_CLIENT_ID=${ANTIGRAVITY_OAUTH_CLIENT_ID};ANTIGRAVITY_OAUTH_CLIENT_SECRET=${ANTIGRAVITY_OAUTH_CLIENT_SECRET}"
    extraenv="${extraenv};ANTIGRAVITY2CODEX_HOST=127.0.0.1"
    if [ -n "$ANTIGRAVITY_LEGACY_CLIENTS" ]; then
      extraenv="${extraenv};ANTIGRAVITY_LEGACY_CLIENTS=${ANTIGRAVITY_LEGACY_CLIENTS}"
    fi
  fi
  label="${LABEL_PREFIX}.${labelsuffix}"
  scriptpath="${FLEET_HOME}/bridges/${bridgedir}/${script}"
  workdir="${FLEET_HOME}/bridges/${bridgedir}"
  if [ ! -f "$scriptpath" ] && [ "$DRY_RUN" != "1" ]; then
    echo "  [warn] missing ${scriptpath}; skipping ${label}" >&2
    continue
  fi
  # A bridge with no key can never answer: the picker would show rows that 401 on
  # every request. Skip the service entirely instead of leaving a dead port,
  # and say which env var to fill in so the next install picks it up.
  if [ -z "$(eval echo \${$keyenv:-})" ]; then
    echo "  [skip] ${label}: ${keyenv} is not set; run 'bash bridges/finish.sh ${name}' after logging in" >&2
    continue
  fi
  if [ "$DRY_RUN" = "1" ]; then
    info "[dry-run] ${label} -> :${port}"
  else
    if [ "$DO_START" = "1" ] && fleet_port_in_use "$port"; then
      echo "  [warn] port ${port} is already in use; ${label} may fail to bind" >&2
    fi
    if [ "$DO_START" = "1" ]; then
      fleet_service_install "$label" "$workdir" "$(service_env "$keyenv" "$(eval echo \${$keyenv:-})" "$extraenv")" \
        "$(bridge_python "$name")" "$scriptpath" "$extra"
      info "started ${label} on :${port}"
    else
      echo "  [info] ${label} -> :${port} (--no-start: not installed)"
    fi
  fi
done

# ---------- 4b. check-in timer (optional) ----------
if [ "$WITH_CHECKIN" = "1" ]; then
  echo "[checkin] installing daily check-in timer"
  if [ "$DRY_RUN" = "1" ]; then
    info "[dry-run] tools/checkin.sh install-timer"
  else
    bash "${FLEET_HOME}/tools/checkin.sh" --home "${FLEET_HOME}" install-timer
  fi
fi

# ---------- 4c. status panel (optional) ----------
if [ "$WITH_UI" = "1" ]; then
  echo "[ui] installing local status panel (port $((PORT_BASE + 9)))"
  if [ "$DRY_RUN" = "1" ]; then
    info "[dry-run] tools/status_ui.sh install-timer"
  else
    bash "${FLEET_HOME}/tools/status_ui.sh" --home "${FLEET_HOME}" install-timer
  fi
fi

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


# ---------- 5b. ocx catalog guard ----------
# Keeps the fleet bridge models in the Codex model catalog. Without it a provider switcher
# (CC Switch) that owns model_catalog_json can strip the bridge models from the picker.
if [ "$WITH_OCX" = "1" ] && [ "$WITH_OCX_GUARD" = "1" ]; then
  echo "[5b/5] ocx catalog guard"
  echo "[5b/6] ocx catalog guard"
  if [ "$DRY_RUN" = "1" ]; then
    info "[dry-run] tools/ocx-catalog-guard.sh install-timer"
  elif command -v ocx >/dev/null 2>&1; then
    bash "${FLEET_HOME}/tools/ocx-catalog-guard.sh" install-timer || \
      echo "  [warn] ocx-catalog-guard timer install failed" >&2
  else
    echo "  [warn] ocx not on PATH; run later: bash ${FLEET_HOME}/tools/ocx-catalog-guard.sh install-timer" >&2
  fi
fi


# ---------- 5c. reachability probe + reachable-first ordering ----------
# Probes every bridge with a real chat call, then re-sorts the catalog so the
# providers that actually answered sit at the top of the picker. Without it the
# ordering is a one-shot snapshot that goes stale as bridges come and go.
if [ "$WITH_OCX" = "1" ] && [ "$WITH_REACH" = "1" ]; then
  echo "[5c/6] fleet reachability probe"
  if [ "$DRY_RUN" = "1" ]; then
    info "[dry-run] tools/fleet-probe-install.sh install"
  elif bash "${FLEET_HOME}/tools/fleet-probe-install.sh" install; then
    bash "${FLEET_HOME}/tools/fleet-probe-install.sh" run || \
      echo "  [warn] first reachability probe failed; ordering unchanged" >&2
  else
    echo "  [warn] reachability probe timer install failed; run later:" >&2
    echo "         bash ${FLEET_HOME}/tools/fleet-probe-install.sh install" >&2
  fi

  # The wrapper keeps ocx sync from wiping the ordering.
  if [ "$DRY_RUN" = "1" ]; then
    info "[dry-run] tools/fleet-sort-after-sync.sh --install"
  elif ! bash "${FLEET_HOME}/tools/fleet-sort-after-sync.sh" --install; then
    echo "  [warn] ocx sort wrapper install failed; ordering may be reset by ocx sync" >&2
  fi
fi

if [ "$DRY_RUN" = "1" ]; then
  echo
  echo "dry run complete; nothing was written."
  exit 0
fi

cat <<DONE

FleetKit v${KIT_VERSION} installed at ${FLEET_HOME}

Next steps
  1. Log in to each service you want (README.md, "logins" table).
  2. After each login:  bash ${FLEET_HOME}/bridges/finish.sh <name>
  3. Health check:      bash ${FLEET_HOME}/tools/status.sh
  4. Status panel:      http://127.0.0.1:$((PORT_BASE + 9))/  (only with --with-ui)
  5. Full chat test:    python3 ${FLEET_HOME}/tools/fleet_chat_test.py

Codex usage: opencodex proxies the bridges at http://127.0.0.1:10100/v1.
Models appear as <bridge>/<model>, e.g. workbuddy/hy4-preview.
DONE

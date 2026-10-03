#!/usr/bin/env bash
# FleetKit installer
#
# Deploys thirteen local reverse-proxy bridges as background services
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
SYNC_ONLY=0

usage() {
  cat <<'USAGE'
FleetKit installer v${KIT_VERSION}

Usage: install.sh [options]

  --home DIR        install root (default: ~/FleetKit/runtime)
  --port-base N     first bridge port; bridges use N..N+16 (default: 8787)
  --with-opencodex  register bridges with opencodex after install (default)
  --no-opencodex    skip opencodex wiring
  --with-checkin   install the daily check-in timer (09:00 CST)
  --with-ui        install the local status panel (port PORT_BASE+9)
  --no-ocx-guard   skip the ocx catalog guard timer (on when opencodex is wired)
  --no-start        write files and plists but do not launch bridges
  --skip-deps       do not create the virtualenv or install Python deps
  --sync-only       copy code from the kit into the install root and stop
                    (no venv, no plists, no restart); the fix for a drifted
                    runtime/, and safe to run while the fleet is live
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
    --sync-only) SYNC_ONLY=1 ;;
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

FLEET_OS_RESOLVED="${FLEET_OS:-}"
if [ -z "$FLEET_OS_RESOLVED" ] && command -v fleet_os >/dev/null 2>&1; then
  FLEET_OS_RESOLVED="$(fleet_os 2>/dev/null || true)"
fi
case "$FLEET_OS_RESOLVED" in
  macos|windows|linux) ;;
  *) FLEET_OS_RESOLVED="$(uname -s 2>/dev/null || true)"
     case "$FLEET_OS_RESOLVED" in
       Darwin*) FLEET_OS_RESOLVED=macos ;;
       Linux*) FLEET_OS_RESOLVED=linux ;;
       MINGW*|MSYS*|CYGWIN*|Windows_NT*) FLEET_OS_RESOLVED=windows ;;
       *) FLEET_OS_RESOLVED=linux ;;
     esac ;;
esac
export FLEET_OS_RESOLVED
echo "  backend    : $FLEET_OS_RESOLVED"

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

# Windows services need native paths: schtasks rejects an msys-style /c/...
# working directory and cmd.exe can neither cd into it nor write to it.
# cygpath leaves native paths untouched, so this is idempotent.
if command -v fleet_is_windows >/dev/null 2>&1 && fleet_is_windows \
   && command -v cygpath >/dev/null 2>&1; then
  case "$FLEET_HOME" in
    /*) FLEET_HOME="$(cygpath -m "$FLEET_HOME")" ;;
  esac
  case "$LOG_DIR" in
    /*) LOG_DIR="$(cygpath -m "$LOG_DIR")" ;;
  esac
  export FLEET_SERVICE_DIR FLEET_LOG_DIR="$LOG_DIR"
fi

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
  "workbuddy|workbuddy2codex|workbuddy-cn|converter.py|0|CODEBUDDY2OPENAI_KEY|--host 127.0.0.1 --port @PORT@|WORKBUDDY_PROVIDER=cn;BRIDGES_DIR=@FLEET_HOME@/bridges"
  # no --auth-dir here on purpose: extra-args is word-split, so a path with
  # a space in FLEET_HOME becomes two argv entries and the bridge dies with
  # "unrecognized arguments". WORKBUDDY_AUTH_POOL_DIR names the same folder.
  "workbuddy-gpt|workbuddy2codex-gpt|workbuddy-gpt|converter.py|1|CODEBUDDY2OPENAI_KEY|--host 127.0.0.1 --port @PORT@|WORKBUDDY_AUTH_POOL_DIR=@FLEET_HOME@/bridges/workbuddy-gpt/auths;WORKBUDDY_LOCAL_STORAGE=@HOME@/.workbuddy-ai/local_storage;WORKBUDDY_PROVIDER=gpt;BRIDGES_DIR=@FLEET_HOME@/bridges"
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
  "kimi-code|kimi2codex|kimi|kimi_bridge.py|15|KIMI2CODEX_KEY|--host 127.0.0.1 --port @PORT@|KIMI_CALL_TIMEOUT=300"
  "minimax|minimax2codex|minimax|minimax_bridge.py|16|MINIMAX2CODEX_KEY|--host 127.0.0.1 --port @PORT@|MINIMAX_CALL_TIMEOUT=300"
)

echo "FleetKit installer v${KIT_VERSION}"
info "kit        : ${KIT_DIR}"
info "fleet home : ${FLEET_HOME}"
info "ports      : ${PORT_BASE} .. $((PORT_BASE + 16))"
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
#
# Only code crosses over. The install root also holds state this script must
# never touch: bridge auth pools (bridges/*/auths), zcode captcha pools,
# finish_setup.sh helpers written during login, *.bak-* rescue copies,
# fleet.env, logs, real_calls.json. copy_tree never deletes, and sync_skip
# keeps the copy one-way for those paths even if a stray copy lands in the kit.
SYNC_CHANGED=""
SYNC_NEW=""
# FLEET_SYNC_PREVIEW=1 reports the drift without writing, so an operator can
# see what a sync would move before letting it touch a live install root.
SYNC_PREVIEW="${FLEET_SYNC_PREVIEW:-0}"

sync_skip() {
  case "$1" in
    __pycache__|__pycache__/*|*/__pycache__|*/__pycache__/*) return 0 ;;
    *.pyc) return 0 ;;
    .DS_Store|*/.DS_Store) return 0 ;;
    .pytest_cache|.pytest_cache/*|*/.pytest_cache|*/.pytest_cache/*) return 0 ;;
    .venv|.venv/*|*/.venv|*/.venv/*) return 0 ;;
    *.bak|*.bak-*|*.bak/*) return 0 ;;
    *.log) return 0 ;;
    # zcode reads the newest human-minted ticket from captcha.txt, so the copy
    # in the install root is live state: overwriting it with the kit's sample
    # would throw away whatever the operator just minted.
    captcha.txt|*/captcha.txt) return 0 ;;
    auths|auths/*|*/auths|*/auths/*) return 0 ;;
    */auths-archive-*|*/auths-archive-*/*) return 0 ;;
    captcha_pool|captcha_pool/*|*/captcha_pool|*/captcha_pool/*) return 0 ;;
    logs|logs/*|*/logs|*/logs/*) return 0 ;;
    fleet.env|real_calls.json|free-windows.json|fleet-reach.json) return 0 ;;
    */fleet.env|*/real_calls.json|*/free-windows.json|*/fleet-reach.json) return 0 ;;
    *) return 1 ;;
  esac
}

copy_tree() {
  local src="$1" dst="$2" item rel target
  [ -d "$src" ] || return 0
  mkdir -p "$dst"
  while IFS= read -r item; do
    rel="${item#"$src"/}"
    target="$dst/$rel"
    sync_skip "$rel" && continue
    if [ -L "$target" ]; then
      info "keeping symlink $target -> $(readlink "$target")"
    elif [ -L "$item" ]; then
      # A kit-side symlink (shared helper, vendored asset) must stay a symlink.
      ln -snf "$(readlink "$item")" "$target" 2>/dev/null \
        && SYNC_CHANGED="${SYNC_CHANGED}${target}
" \
        || info "warn: cannot link $target"
    elif [ -d "$item" ] && [ ! -L "$item" ]; then
      # Create the directory only. A bulk cp -R here would drag __pycache__ and
      # anything else sync_skip refuses straight into the install root; copying
      # file by file is what makes that list the single gate.
      [ -d "$target" ] || mkdir -p "$target"
    elif [ -e "$target" ] && cmp -s "$item" "$target"; then
      # Drift is the whole reason --sync-only exists, so name what moved and
      # leave identical files alone: a 5 minute timer re-runs this often.
      continue
    else
      if [ "$SYNC_PREVIEW" = "1" ]; then
        [ -e "$target" ] || SYNC_NEW="${SYNC_NEW}${target}
"
        SYNC_CHANGED="${SYNC_CHANGED}${target}
"
        continue
      fi
      [ -e "$target" ] || SYNC_NEW="${SYNC_NEW}${target}
"
      cp "$item" "$target" || { info "warn: cannot copy $item"; continue; }
      SYNC_CHANGED="${SYNC_CHANGED}${target}
"
    fi
  done < <(find "$src" -mindepth 1 | sort)
}

# bridges/workbuddy/ is the single copy of the shared WorkBuddy modules and the
# dashboard logo. An install root created before that refactor still has them
# inside bridges/workbuddy-cn/ and bridges/workbuddy-gpt/, and copy_tree never
# deletes: Python would then load whichever copy came first. Strip exactly this
# allowlist, only in those two directories -- state (auths/, bridge-settings.json,
# assets/, captcha.txt) and rescue copies are never touched.
SHARED_WB_NAMES="account_pool.py dashboard.py desensitize.py wbb.py workbuddy_account_service.py workbuddy_checkin.py bridge-logo.png"
SYNC_PRUNED=""

prune_shared_workbuddy() {
  local bridges="$1" dir name path
  for dir in workbuddy-cn workbuddy-gpt; do
    for name in $SHARED_WB_NAMES; do
      path="$bridges/$dir/$name"
      [ -e "$path" ] || continue
      find "$path" -maxdepth 0 -delete 2>/dev/null \
        || { info "warn: cannot prune $path"; continue; }
      SYNC_PRUNED="${SYNC_PRUNED}${path}
"
    done
  done
  return 0
}

sync_report() {
  local count=0 f
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    count=$((count + 1))
  done <<EOF
$SYNC_CHANGED
EOF
  info "sync: ${count} file(s) copied into ${FLEET_HOME}"
  if [ "${FLEET_SYNC_VERBOSE:-0}" = "1" ]; then
    printf '%s' "$SYNC_CHANGED" | while IFS= read -r f; do
      [ -n "$f" ] || continue
      info "  $f"
    done
  fi
  if [ -n "$SYNC_NEW" ]; then
    info "sync: newly created:"
    printf '%s' "$SYNC_NEW" | while IFS= read -r f; do
      [ -n "$f" ] || continue
      info "  $f"
    done
  fi
  if [ -n "$SYNC_PRUNED" ]; then
    info "sync: pruned $(printf '%s' "$SYNC_PRUNED" | grep -c .) stale file(s) now shared via bridges/workbuddy/"
    printf '%s' "$SYNC_PRUNED" | while IFS= read -r f; do
      [ -n "$f" ] || continue
      info "  $f"
    done
  fi
}

# Same one-way copy for the loose top-level files, with the same "did it move"
# bookkeeping and the same refusal to clobber an operator's rescue copy.
copy_file() {
  local src="$1" dst="$2"
  [ -f "$src" ] || return 0
  if [ -e "$dst" ] && cmp -s "$src" "$dst"; then
    return 0
  fi
  [ -e "$dst" ] || SYNC_NEW="${SYNC_NEW}${dst}
"
  cp "$src" "$dst" || { info "warn: cannot copy $src"; return 0; }
  SYNC_CHANGED="${SYNC_CHANGED}${dst}
"
}

# cp keeps the destination's mode, so a script the kit marks executable can end
# up non-executable in the install root and launchd dies with 126. Mirror the
# kit's own bits instead of blanket chmod +x: several tools are intentionally
# not executable.
sync_modes() {
  local f rel target
  for f in "${KIT_DIR}/tools"/* "${KIT_DIR}/bridges"/*/*; do
    [ -f "$f" ] || continue
    rel="${f#"${KIT_DIR}"/}"
    target="${FLEET_HOME}/$rel"
    [ -f "$target" ] || continue
    if [ -x "$f" ]; then
      [ -x "$target" ] || chmod +x "$target" 2>/dev/null || true
    else
      [ -x "$target" ] && chmod -x "$target" 2>/dev/null || true
    fi
  done
  return 0
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
  info "would copy bridges/ tools/ docs/ opencodex/ README.md requirements.txt install.sh uninstall.sh"
else
  copy_tree "${KIT_DIR}/bridges" "${FLEET_HOME}/bridges"
  copy_tree "${KIT_DIR}/tools" "${FLEET_HOME}/tools"
  copy_tree "${KIT_DIR}/docs" "${FLEET_HOME}/docs"
  copy_tree "${KIT_DIR}/opencodex" "${FLEET_HOME}/opencodex"
  # copy_tree never deletes, so moving the shared WorkBuddy modules into
  # bridges/workbuddy/ would otherwise leave stale duplicates behind and
  # Python would load whichever copy came first. Remove exactly that
  # allowlist (never a wildcard) so only the shared copy survives.
  prune_shared_workbuddy "${FLEET_HOME}/bridges"
  copy_file "${KIT_DIR}/README.md" "${FLEET_HOME}/README.md"
  copy_file "${KIT_DIR}/requirements.txt" "${FLEET_HOME}/requirements.txt"
  # tools/free_models.py reads this from the fleet home, so it must travel too
  copy_file "${KIT_DIR}/free-windows.json" "${FLEET_HOME}/free-windows.json"
  copy_file "${KIT_DIR}/uninstall.sh" "${FLEET_HOME}/uninstall.sh"
  # install.sh sits at the kit root, outside the four synced trees, so
  # nothing ever refreshed this copy. tools/test_install_dry_run.py reads
  # the one beside it, so the install root's suite went red on a stale
  # snapshot while the kit's own copy was already fixed.
  copy_file "${KIT_DIR}/install.sh" "${FLEET_HOME}/install.sh"
  if [ -f "${FLEET_HOME}/uninstall.sh" ]; then
    chmod +x "${FLEET_HOME}/uninstall.sh"
  fi
  sync_modes
fi

if [ "$SYNC_ONLY" = "1" ]; then
  sync_report
  info "sync-only: skipped virtualenv, services and opencodex wiring"
  echo "  hint: restart the moved services, e.g."
  echo "    launchctl kickstart -k gui/$(id -u)/${LABEL_PREFIX}.fleet-ui"
  exit 0
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
VENV_PY="${FLEET_HOME}/.venv/bin/python"
if command -v fleet_is_windows >/dev/null 2>&1 && fleet_is_windows; then
  VENV_PY="${FLEET_HOME}/.venv/Scripts/python.exe"
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
    # FLEET_PYTHON/VENV_PIP/VENV_PY above were resolved BEFORE the venv existed,
    # so a fresh install always fell through to the posix layout (bin/) even on
    # Windows. Now that the layout is real, re-resolve all three: a Windows venv
    # keeps pip/python under Scripts/, and bin/pip does not exist there.
    if [ -x "${FLEET_HOME}/.venv/Scripts/pip.exe" ]; then
      VENV_PIP="${FLEET_HOME}/.venv/Scripts/pip.exe"
      VENV_PY="${FLEET_HOME}/.venv/Scripts/python.exe"
      FLEET_PYTHON="${FLEET_HOME}/.venv/Scripts/python.exe"
    elif [ -x "${FLEET_HOME}/.venv/bin/pip" ]; then
      VENV_PIP="${FLEET_HOME}/.venv/bin/pip"
      VENV_PY="${FLEET_HOME}/.venv/bin/python"
      FLEET_PYTHON="${FLEET_HOME}/.venv/bin/python"
    fi
    # pip cannot overwrite its own console script on Windows ("ERROR: To modify
    # pip, please run: python.exe -m pip install --upgrade pip"); under `set -e`
    # that aborts the whole install. Drive it through the interpreter, and never
    # fail here: upgrading pip is hygiene, the real dependency install is next.
    "$VENV_PY" -m pip install --quiet --upgrade pip || true
    "$VENV_PY" -m pip install --quiet -r "${KIT_DIR}/requirements.txt"
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
  for cand in "${HOME}"/.local/node-*/lib/node_modules/*/.venv/Scripts/python.exe \
              "${HOME}"/.local/node-*/lib/node_modules/*/.venv/bin/python \
              "${HOME}"/.local/share/pnpm/global/*/node_modules/*/.venv/Scripts/python.exe \
              "${HOME}"/.local/share/pnpm/global/*/node_modules/*/.venv/bin/python \
              "${FLEET_HOME}"/bridges/*/.venv/Scripts/python.exe \
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

# pick_key mints a placeholder when the operator never set one, which is right
# for a local bridge key (any string works) but wrong for an upstream API key:
# a fabricated Qwen credential would 401 on every chat call. Keep it empty.
pick_optional() {
  local want="$1" value=""
  if [ -n "$EXISTING" ]; then
    value="$(echo "$EXISTING" | grep -m1 "^${want}=" | cut -d= -f2- | tr -d '"' || true)"
  fi
  echo "$value"
}

QWEN_API_KEY="$(pick_optional QWEN_API_KEY)"

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
KIMI2CODEX_KEY="$(pick_key KIMI2CODEX_KEY)"
MINIMAX2CODEX_KEY="$(pick_key MINIMAX2CODEX_KEY)"

# Antigravity google oauth client pair is never committed to git (push protection
# rejects it) and every install ships it in its own binary, so read it from there.
# Same "operator-owned, never minted" rule for the Antigravity google oauth
# client pair: it is deliberately not committed to git (push protection rejects
# it), so the only source is the previous fleet.env.
ANTIGRAVITY_OAUTH_CLIENT_ID="$(pick_optional ANTIGRAVITY_OAUTH_CLIENT_ID)"
ANTIGRAVITY_OAUTH_CLIENT_SECRET="$(pick_optional ANTIGRAVITY_OAUTH_CLIENT_SECRET)"
ANTIGRAVITY_LEGACY_CLIENTS="$(pick_optional ANTIGRAVITY_LEGACY_CLIENTS)"

if [ -z "$ANTIGRAVITY_OAUTH_CLIENT_ID" ] || [ -z "$ANTIGRAVITY_OAUTH_CLIENT_SECRET" ]; then
  AGY_BIN="${AGY_BIN:-/Applications/Antigravity.app/Contents/Resources/bin/language_server}"
  # --verify refreshes the jetski token against oauth2.googleapis.com, once per
  # candidate pair with a 25s timeout each. A dry run is a plan, not a probe:
  # behind a blocking network it turns a 1s local scan into minutes of hang.
  AGY_VERIFY="--verify"
  if [ "$DRY_RUN" = "1" ]; then AGY_VERIFY=""; fi
  AGY_LINES="$(python3 "${KIT_DIR}/bridges/antigravity/extract_client.py" $AGY_VERIFY "$AGY_BIN" 2>/dev/null || true)"
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

# gemini2codex refreshes against the same oauth2.googleapis.com endpoint and
# accepts any Google consumer pair Antigravity ships, so it reuses the pair
# above instead of baking a second copy (and a second revocation risk) in git.
GEMINI_OAUTH_CLIENT_ID="$(pick_optional GEMINI_OAUTH_CLIENT_ID)"
GEMINI_OAUTH_CLIENT_SECRET="$(pick_optional GEMINI_OAUTH_CLIENT_SECRET)"
if [ -z "$GEMINI_OAUTH_CLIENT_ID" ] && [ -n "$ANTIGRAVITY_OAUTH_CLIENT_ID" ]; then
  GEMINI_OAUTH_CLIENT_ID="$ANTIGRAVITY_OAUTH_CLIENT_ID"
  GEMINI_OAUTH_CLIENT_SECRET="$ANTIGRAVITY_OAUTH_CLIENT_SECRET"
fi
emit_fleet_env() {
  local preserved=""
  if [ -n "$EXISTING" ]; then
    # Operator-owned keys are not managed by install.sh: carry the exact
    # previous lines across so a re-install never drops them.
    # Operator-owned settings, not managed by install.sh: carry the exact
    # previous lines across so a re-install never drops them.
    # FLEET_DEFAULT_MODEL matters as much as a key -- losing it silently reverts
    # the Codex picker default to the hardcoded fallback.
    preserved="$(echo "$EXISTING" | grep -E '^(HOMEBREW_PYTHON|TOKENDANCE_API_KEY|STEPFUN_PLAN_API_KEY|FLEET_DEFAULT_MODEL)=' || true)"
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
# The service backend this home was installed with. Child tools read it
# instead of sniffing the host, so a linux backend on Windows keeps working.
FLEET_OS="${FLEET_OS_RESOLVED}"
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
# Optional: real Qwen Cloud key. Empty means the bridge has no upstream key at
# all: /v1/models serves the static fallback catalog and every chat call fails
# locally with qwen_key_missing until a real key is set here.
QWEN_API_KEY="${QWEN_API_KEY}"
CLINE2CODEX_KEY="${CLINE2CODEX_KEY}"
ZCODE2CODEX_KEY="${ZCODE2CODEX_KEY}"
KIMI2CODEX_KEY="${KIMI2CODEX_KEY}"
MINIMAX2CODEX_KEY="${MINIMAX2CODEX_KEY}"
ANTIGRAVITY_OAUTH_CLIENT_ID="${ANTIGRAVITY_OAUTH_CLIENT_ID}"
ANTIGRAVITY_OAUTH_CLIENT_SECRET="${ANTIGRAVITY_OAUTH_CLIENT_SECRET}"
# Optional extra id:secret pairs tried after the primary (Antigravity rotates these).
ANTIGRAVITY_LEGACY_CLIENTS="${ANTIGRAVITY_LEGACY_CLIENTS}"

# gemini2codex shares the Antigravity Google OAuth pair: the bridge reads
# GEMINI_OAUTH_CLIENT_ID/SECRET first (bridges/gemini/gemini_bridge.py) and a
# revoked pair fails refresh with 401, which is what took this bridge down.
GEMINI_OAUTH_CLIENT_ID="${GEMINI_OAUTH_CLIENT_ID}"
GEMINI_OAUTH_CLIENT_SECRET="${GEMINI_OAUTH_CLIENT_SECRET}"

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
  # PATH is intentionally not an env pair: the wrapper writers emit it as a
  # dedicated line, because a native Windows PATH carries ';' -- the same
  # separator the env-pair list uses.
  local home
  home="$(fleet_home_win 2>/dev/null || printf '%s' "$HOME")"
  printf '%s' "HOME=${home};${keyenv}=${keyval};${out}"
}

HOME_WIN="$(fleet_home_win 2>/dev/null || printf '%s' "$HOME")"
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
  extra="${extra//@HOME@/$HOME_WIN}"
  extraenv="${extraenv//@HOME@/$HOME_WIN}"
  if [ "$name" = "antigravity" ] && [ -n "$ANTIGRAVITY_OAUTH_CLIENT_ID" ]; then
    extraenv="${extraenv};ANTIGRAVITY_OAUTH_CLIENT_ID=${ANTIGRAVITY_OAUTH_CLIENT_ID};ANTIGRAVITY_OAUTH_CLIENT_SECRET=${ANTIGRAVITY_OAUTH_CLIENT_SECRET}"
    extraenv="${extraenv};ANTIGRAVITY2CODEX_HOST=127.0.0.1"
    if [ -n "$ANTIGRAVITY_LEGACY_CLIENTS" ]; then
      extraenv="${extraenv};ANTIGRAVITY_LEGACY_CLIENTS=${ANTIGRAVITY_LEGACY_CLIENTS}"
    fi
  fi
  if [ "$name" = "gemini" ] && [ -n "$GEMINI_OAUTH_CLIENT_ID" ]; then
    extraenv="${extraenv};GEMINI_OAUTH_CLIENT_ID=${GEMINI_OAUTH_CLIENT_ID};GEMINI_OAUTH_CLIENT_SECRET=${GEMINI_OAUTH_CLIENT_SECRET}"
  fi
  if [ "$name" = "qwen" ] && [ -n "$QWEN_API_KEY" ]; then
    extraenv="${extraenv};QWEN_API_KEY=${QWEN_API_KEY}"
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

# ---------- 4d. stepfun image-cap shim ----------
# StepFun's Plan API refuses a 71st image with 400 images_too_many, and Codex
# re-sends its whole history every turn (tools/image_cap.py holds the
# measurement).
# The shim de-duplicates and caps photos *below* CC Switch on 15722 and forwards
# everything else byte for byte, so it stays harmless on a fleet that has no
# stepfun provider registered. Below CC Switch, not in front of it: measured
# 2026-09-30, the in-front arrangement lost to CC Switch owning
# ~/.codex/config.toml, which rewrote the pinned base_url back to its own port
# while Codex was already running. setup-providers.sh re-points CC Switch's
# routing table at this port; CC Switch has to be restarted to pick that up.
echo "[shim] installing stepfun image-cap shim (127.0.0.1:15722 -> https://api.stepfun.com/step_plan/v1)"
if [ "$DRY_RUN" = "1" ]; then
  info "[dry-run] tools/stepfun_image_shim.sh install-timer"
else
  bash "${FLEET_HOME}/tools/stepfun_image_shim.sh" --home "${FLEET_HOME}" install-timer || \
    echo "  [warn] stepfun image-cap shim install failed; run it later: bash ${FLEET_HOME}/tools/stepfun_image_shim.sh install-timer" >&2
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

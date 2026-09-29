#!/usr/bin/env bash
# catalog-filter.sh - drop the Codex picker entries for bridges that are not working.
#
# Why this exists
# ---------------
# ocx sync writes one catalog entry per advertised bridge model, so a bridge that is
# down stays selectable in the Codex model picker: you pick qoder/Auto, the request
# fails, and nothing in the picker told you why. The status panel already knows which
# bridges really work (verify_real_calls.py verdicts), so this wrapper replays that
# verdict onto the catalog and hides the broken rows.
#
# It is the inverse half of ocx-catalog-guard.sh: that one puts bridge models back when
# they vanish, this one takes the dead bridges out again. A provider that verifies REAL
# but lost its rows triggers one cooldown-limited ocx sync, so recovery is not permanent.
#
# Usage:
#   catalog-filter.sh run              filter the catalog (default)
#   catalog-filter.sh install-timer    launchd timer, default every 300s
#   catalog-filter.sh uninstall-timer
#   catalog-filter.sh status
#
# Options:
#   --codex-home DIR   Codex home (default: ~/.codex, honours $CODEX_HOME)
#   --status-url URL   fleet status panel
#   --keep P[,P...]    providers to keep even when unavailable
#   --interval SEC     timer interval in seconds (default: 300)
#   --log FILE         log file (default: ~/Library/Logs/catalog-filter.log)
#   --dry-run          report what would be dropped, change nothing
#   --report-only      report what would be dropped, change nothing
#   -h | --help
set -euo pipefail

INTERVAL=300
CODEX_HOME_DIR="${CODEX_HOME:-${HOME}/.codex}"
STATUS_URL="${FLEET_STATUS_URL:-http://127.0.0.1:8796/api/status}"
KEEP_PROVIDERS=""
HIDE_NATIVE=0
HIDE_ENABLED=0
REPORT_ONLY=0
PROXY_BASE="${FLEET_PROXY_BASE:-http://127.0.0.1:10100}"
LOG_FILE="${CATALOG_FILTER_LOG:-${HOME}/Library/Logs/catalog-filter.log}"
# Defined before anything sources platform.sh: platform.sh lives beside this
# script, and `set -u` turns a use-before-definition into an immediate exit.
# Resolve without dirname for the same reason as ocx-catalog-guard.sh: a
# Task Scheduler action inherits a PATH with no Git for Windows coreutils.
case "$0" in
  */*) SCRIPT_DIR="$(cd "${0%/*}" && pwd)" ;;
  *) SCRIPT_DIR="$(pwd)" ;;
esac
# Platform abstraction: launchd / Task Scheduler / Linux supervisor.
if [ -f "${SCRIPT_DIR}/platform.sh" ]; then
  # shellcheck source=platform.sh
  . "${SCRIPT_DIR}/platform.sh"
fi
if command -v fleet_service_dir >/dev/null 2>&1; then
  LAUNCH_DIR="$(fleet_service_dir)"
fi
LAUNCH_DIR="${FLEET_LAUNCH_DIR:-${LAUNCH_DIR:-${HOME}/Library/LaunchAgents}}"
FLEET_SERVICE_DIR="${FLEET_SERVICE_DIR:-${LAUNCH_DIR}}"
export FLEET_SERVICE_DIR
LABEL_PREFIX="${FLEET_LABEL_PREFIX:-com.local}"
DRY_RUN=0
ACTION=run

FILTER="${CATALOG_FILTER_PY:-${SCRIPT_DIR}/catalog_filter.py}"

usage() {
  cat <<USAGE
catalog-filter.sh - hide Codex picker models for bridges that are not working

  run              filter the catalog (default)
  install-timer    launchd timer, every 300s by default
  uninstall-timer  remove the launchd timer
  status           catalog counts, timer state, last log lines

Options:
  --codex-home DIR    Codex home (default ~/.codex)
  --status-url URL    fleet status panel
  --keep P[,P...]     providers to keep even when unavailable
  --interval SEC      timer interval in seconds (default 300)
  --log FILE          log file
  --dry-run           report only
  --proxy-base URL    local proxy base used for the native-pool probe
  --hide-native-when-pool-down
                      hide the unprefixed native picker rows when the local proxy
                      reports its account pool has no usable credential
USAGE
}

log() {
  echo "$(date "+%Y-%m-%d %H:%M:%S") $*" >>"$LOG_FILE" 2>/dev/null || true
}

# One line per run, so a year of 300s ticks stays readable.
summarise() {
  python3 - "$1" <<'PY'
import json, sys

try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
print("removed=%s after=%s unavailable=%s restored=%s" % (
    d.get("removed"), d.get("models_after"),
    ",".join(d.get("unavailable") or []) or "-",
    ",".join(d.get("restored") or []) or "-"))
PY
}

cmd_run() {
  mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true
  if [ ! -f "$FILTER" ]; then
    log "skip: $FILTER not found"
    return 0
  fi

  local report
  report="$(mktemp "${TMPDIR:-/tmp}/catalog-filter.XXXXXX")"
  local args=(--codex-home "$CODEX_HOME_DIR" --status-url "$STATUS_URL")
  [ -n "$KEEP_PROVIDERS" ] && args+=(--keep "$KEEP_PROVIDERS")
  [ "$DRY_RUN" = "1" ] && args+=(--dry-run)
  [ "$REPORT_ONLY" = "1" ] && args+=(--report-only)
  [ "$HIDE_NATIVE" = "1" ] && args+=(--hide-native-when-pool-down --proxy-base "$PROXY_BASE")

  local rc=0
  if ! python3 "$FILTER" "${args[@]}" >"$report" 2>&1; then
    rc=$?
  fi
  if [ "$rc" = "0" ]; then
    log "ok: $(summarise "$report")"
  else
    # rc 3 is the panel being down, rc 4 is nothing verified REAL: both mean the
    # filter could not tell what is broken, so it wrote nothing. Not an alarm.
    if [ "$rc" = "3" ] || [ "$rc" = "4" ]; then
      log "skip (rc=$rc): $(head -n 1 "$report")"
    else
      log "FAILED (rc=$rc): $(tail -n 2 "$report" | tr '\n' ' ')"
    fi
  fi
  if [ "$DRY_RUN" = "1" ]; then
    cat "$report"
  fi
  return 0
}

cmd_install_timer() {
  local label="${LABEL_PREFIX}.catalog-filter"
  local path_value="${CATALOG_FILTER_PATH:-${PATH:-/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin}}"
  local ocx_bin
  ocx_bin="$(command -v ocx 2>/dev/null || true)"
  if [ -n "$ocx_bin" ]; then
    path_value="$(dirname "$ocx_bin"):${path_value}"
  fi
  local ocx_path="$path_value"
  local st
  st="$(command -v bash 2>/dev/null || echo /bin/bash)"
  fleet_timer_install "$label" "$INTERVAL" "$st" "${SCRIPT_DIR}/catalog-filter.sh" "run --hide-native-when-pool-down"
  echo "installed ${label} (every ${INTERVAL}s) on $(fleet_os 2>/dev/null || echo macos): ${ocx_path}"
}

cmd_uninstall_timer() {
  local label="${LABEL_PREFIX}.catalog-filter"
  fleet_service_remove "$label"
  echo "removed ${label}"
}

cmd_status() {
  local label="${LABEL_PREFIX}.catalog-filter"
  python3 - "$CODEX_HOME_DIR" <<'PY'
import json, os, sys

home = sys.argv[1]
name = None
try:
    with open(os.path.join(home, "config.toml"), encoding="utf-8") as fh:
        for line in fh:
            s = line.strip()
            if s.startswith("model_catalog_json"):
                name = s.split("=", 1)[1].strip().strip(chr(34))
                break
except OSError:
    pass
if not name:
    print("codex home  : %s (no model_catalog_json)" % home)
    sys.exit(0)
path = name if os.path.isabs(name) else os.path.join(home, name)
try:
    models = json.load(open(path, encoding="utf-8")).get("models") or []
except Exception as exc:
    print("codex home  : %s" % home)
    print("catalog     : %s (unreadable: %s)" % (path, exc))
    sys.exit(0)
slashed = [m for m in models
          if "/" in str(m.get("slug") or m.get("id") or "")]
providers = {}
for m in slashed:
    slug = str(m.get("slug") or "")
    providers[slug.split("/", 1)[0]] = providers.get(slug.split("/", 1)[0], 0) + 1


print("codex home  : %s" % home)
print("catalog     : %s" % path)
print("models      : %d total, %d bridged" % (len(models), len(slashed)))
print("providers   : %s" % (", ".join("%s=%d" % kv for kv in sorted(providers.items())) or "-"))
PY
  echo "log         : ${LOG_FILE}"
  # The installed definition is the source of truth on every platform.
  if [ -f "${LAUNCH_DIR}/${label}.plist" ]; then
    interval_kv="$(awk '/<key>StartInterval<\/key>/{f=1;next} f&&/<integer>/{gsub(/<[^>]*>/,"");sub(/^ +/,"");sub(/ +$/,"");print;exit}' "${LAUNCH_DIR}/${label}.plist")"
    if [ -n "$interval_kv" ] && [ "$interval_kv" -gt 0 ] 2>/dev/null; then
      echo "timer       : installed (every ${interval_kv}s)"
    else
      echo "timer       : BROKEN (no StartInterval; re-run install-timer)"
    fi
  elif fleet_service_exists "$label"; then
    echo "timer       : installed (every ${INTERVAL}s, $(fleet_os 2>/dev/null || echo macos))"
  else
    echo "timer       : not installed"
  fi
  if [ -f "$LOG_FILE" ]; then
    echo "last log lines:"
    tail -n 5 "$LOG_FILE"
  fi
  return 0
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    run) ACTION=run ;;
    install-timer) ACTION=install-timer ;;
    uninstall-timer) ACTION=uninstall-timer ;;
    status) ACTION=status ;;
    --codex-home) shift; CODEX_HOME_DIR="${1:?--codex-home needs a value}" ;;
    --codex-home=*) CODEX_HOME_DIR="${1#*=}" ;;
    --status-url) shift; STATUS_URL="${1:?--status-url needs a value}" ;;
    --status-url=*) STATUS_URL="${1#*=}" ;;
    --keep) shift; KEEP_PROVIDERS="${1:?--keep needs a value}" ;;
    --keep=*) KEEP_PROVIDERS="${1#*=}" ;;
    --interval) shift; INTERVAL="${1:?--interval needs a value}" ;;
    --interval=*) INTERVAL="${1#*=}" ;;
    --log) shift; LOG_FILE="${1:?--log needs a value}" ;;
    --log=*) LOG_FILE="${1#*=}" ;;
    --dry-run) DRY_RUN=1 ;;
    --report-only) REPORT_ONLY=1; DRY_RUN=1 ;;
    --hide-native-when-pool-down) HIDE_NATIVE=1 ;;
    --report-only) REPORT_ONLY=1 ;;
    --proxy-base) shift; PROXY_BASE="${1:?--proxy-base needs a value}" ;;
    --proxy-base=*) PROXY_BASE="${1#*=}" ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

case "$ACTION" in
  run) cmd_run ;;
  install-timer) cmd_install_timer ;;
  uninstall-timer) cmd_uninstall_timer ;;
  status) cmd_status ;;
esac

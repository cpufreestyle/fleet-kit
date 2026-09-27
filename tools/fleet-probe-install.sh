#!/usr/bin/env bash
# Install a launchd timer that periodically re-measures fleet reachability.
#
# Why a timer instead of an inline call: a full probe makes a real chat call
# per bridge and takes minutes, so it must never block `ocx sync`. The wrapper
# (fleet-sort-after-sync.sh) only kicks this timer when the snapshot is stale.
#
#   tools/fleet-probe-install.sh            install/update + start
#   tools/fleet-probe-install.sh status     show state
#   tools/fleet-probe-install.sh run        probe once in the foreground
#   tools/fleet-probe-install.sh uninstall  remove the timer
set -euo pipefail

KIT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.local.fleet-probe"
PLIST="${FLEET_LAUNCH_DIR:-$HOME/Library/LaunchAgents}/${LABEL}.plist"
PYTHON="${FLEET_PYTHON:-$KIT/../runtime/.venv/bin/python}"
PROBE="$KIT/tools/fleet_probe.py"
REACH="${FLEET_REACH_FILE:-$HOME/.codex/fleet-reach.json}"
ENV_FILE="${FLEET_ENV_FILE:-$KIT/../runtime/fleet.env}"
LOG="${FLEET_PROBE_LOG:-$HOME/Library/Logs/fleet-probe.log}"
INTERVAL="${FLEET_PROBE_INTERVAL:-1800}"

write_plist() {
    mkdir -p "$(dirname "$PLIST")" "$(dirname "$LOG")"
    {
        echo "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
        echo "<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">"
        echo "<plist version=\"1.0\"><dict>"
        echo "<key>Label</key><string>$LABEL</string>"
        echo "<key>ProgramArguments</key><array>"
        echo "    <string>$PYTHON</string>"
        echo "    <string>$PROBE</string>"
        echo "    <string>--out</string><string>$REACH</string>"
        echo "    <string>--tries</string><string>6</string>"
        echo "    <string>--call-timeout</string><string>8</string>"
        echo "    <string>--sort-after</string>"
        echo "</array>"
        echo "<key>EnvironmentVariables</key><dict>"
        echo "    <key>FLEET_ENV_FILE</key><string>$ENV_FILE</string>"
        echo "</dict>"
        echo "<key>StartInterval</key><integer>$INTERVAL</integer>"
        echo "<key>RunAtLoad</key><true/>"
        echo "<key>StandardOutPath</key><string>$LOG</string>"
        echo "<key>StandardErrorPath</key><string>$LOG</string>"
        echo "</dict></plist>"
    } > "$PLIST"
}

install_timer() {
    write_plist
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    launchctl kickstart -k "gui/$(id -u)/$LABEL"
    echo "installed $LABEL every ${INTERVAL}s"
    echo "  plist: $PLIST"
    echo "  log  : $LOG"
}

uninstall_timer() {
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    if [ -f "$PLIST" ]; then
        mv "$PLIST" "$PLIST.disabled"
        echo "removed $LABEL (plist kept as .disabled)"
    else
        echo "$LABEL not installed"
    fi
}

status_timer() {
    # `set -e` plus `grep -q` in a pipeline aborts the function, so capture first
    local listing
    listing="$(launchctl list 2>/dev/null || true)"
    case "$listing" in
        *"$LABEL"*) echo "$LABEL: loaded" ;;
        *)          echo "$LABEL: not loaded" ;;
    esac
    if [ -f "$REACH" ]; then
        echo "  snapshot: $REACH"
        "$PYTHON" -c "import json,sys;d=json.load(open(sys.argv[1],encoding=\"utf-8\"));print(\"    measured_at:\",d.get(\"measured_at\"));print(\"    reachable  :\",\", \".join(d.get(\"reachable\") or []));print(\"    unreachable:\",\", \".join(d.get(\"unreachable\") or []))" "$REACH"
    else
        echo "  snapshot: missing ($REACH)"
    fi
}

run_once() {
    FLEET_ENV_FILE="$ENV_FILE" "$PYTHON" "$PROBE" --out "$REACH" \
        --tries 6 --call-timeout 8
}

case "${1:-install}" in
    install)   install_timer ;;
    uninstall) uninstall_timer ;;
    status)    status_timer ;;
    run)       run_once ;;
    *) echo "usage: $0 [install|uninstall|status|run]" >&2; exit 2 ;;
esac

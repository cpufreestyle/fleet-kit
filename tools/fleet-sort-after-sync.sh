#!/usr/bin/env bash
# Re-apply the reachable-first catalog ordering after any ocx sync.
#
# `ocx sync` rebuilds the catalog from scratch and resets every entry's
# priority to 5, which throws away the ordering catalog_sort.py applied.
# ocx exposes no hook, so this wrapper runs the real ocx and, when the
# subcommand was sync-ish, re-sorts and refreshes the cache afterwards.
#
# Install once:   fleet-sort-after-sync.sh --install
# Undo:           fleet-sort-after-sync.sh --uninstall
set -uo pipefail

KIT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="${BASH_SOURCE[0]}"
if [ -L "$SRC" ]; then
    TGT="$(readlink "$SRC")"
    case "$TGT" in
        /*) SRC="$TGT" ;;
        *)  SRC="$(dirname "$SRC")/$TGT" ;;
    esac
fi
KIT="$(cd "$(dirname "$SRC")/.." && pwd)"
REAL_OCX="${FLEET_REAL_OCX:-/Users/a1-6/.local/node-v22.20.0-darwin-arm64/lib/node_modules/@bitkyc08/opencodex/bin/ocx.mjs}"
OCX_BIN="${FLEET_OCX_BIN:-/Users/a1-6/.local/node-v22.20.0-darwin-arm64/bin/ocx}"
OCX_ALIAS="${FLEET_OCX_ALIAS:-/Users/a1-6/.local/node-v22.20.0-darwin-arm64/bin/opencodex}"
REACH="${FLEET_REACH_FILE:-$KIT/tools/fleet-reach.json}"
PROBE="${FLEET_PROBE:-$KIT/tools/fleet_probe.py}"
REACH_MAX_AGE="${FLEET_REACH_MAX_AGE:-86400}"
ENV_FILE="${FLEET_ENV_FILE:-$KIT/../runtime/fleet.env}"
PYTHON="${FLEET_PYTHON:-$KIT/../runtime/.venv/bin/python}"
SELF="$(cd "$(dirname "$SRC")" && pwd)/$(basename "$SRC")"

run_real() {
    if command -v bun >/dev/null 2>&1; then
        bun "$REAL_OCX" "$@"
    else
        node "$REAL_OCX" "$@"
    fi
}

link_one() {
    # $1 = bin path to (re)point at this wrapper; only symlinks are touched
    local bin="$1"
    if [ -L "$bin" ] || [ ! -e "$bin" ]; then
        ln -sfn "$SELF" "$bin.new" && mv -f "$bin.new" "$bin"
        echo "  $bin -> $SELF"
        return 0
    fi
    echo "  $bin is a real file, skipped" >&2
    return 1
}

unlink_one() {
    local bin="$1"
    if [ -L "$bin" ] && [ "$(readlink "$bin")" = "$SELF" ]; then
        ln -sfn "$REAL_OCX" "$bin.new" && mv -f "$bin.new" "$bin"
        echo "  $bin -> $REAL_OCX"
        return 0
    fi
    echo "  $bin is not our wrapper, skipped" >&2
    return 1
}

if [ ! -f "$REAL_OCX" ]; then
    echo "fleet-sort: real ocx not found at $REAL_OCX" >&2
    exit 127
fi

sub="${1:-}"
case "$sub" in
    --install)
        echo "fleet-sort: installing wrapper"
        rc=0
        link_one "$OCX_BIN" || rc=1
        link_one "$OCX_ALIAS" || rc=1
        exit $rc
        ;;
    --uninstall)
        echo "fleet-sort: removing wrapper"
        rc=0
        unlink_one "$OCX_BIN" || rc=1
        unlink_one "$OCX_ALIAS" || rc=1
        exit $rc
        ;;
esac

run_real "$@"
rc=$?

needs_sort=0
case "$sub" in
    sync|sync-cache) needs_sort=1 ;;
esac

# seconds since the reachability snapshot was written; big when missing
reach_age() {
    [ -f "$REACH" ] || { echo 999999; return; }
    local now mt
    now=$(date +%s)
    mt=$(stat -f %m "$REACH" 2>/dev/null || echo 0)
    echo $(( now - mt ))
}

# refresh the snapshot when it is older than REACH_MAX_AGE
refresh_reach() {
    [ -x "$PYTHON" ] && [ -f "$PROBE" ] || return 1
    local age
    age=$(reach_age)
    [ "$age" -le "$REACH_MAX_AGE" ] && return 0
    echo "fleet-sort: snapshot is ${age}s old, re-probing in background" >&2
    # launchd, not a detached job: this sandbox reaps background children
    local plist="${FLEET_LAUNCH_DIR:-$HOME/Library/LaunchAgents}/com.local.fleet-probe.plist"
    if [ -f "$plist" ]; then
        launchctl kickstart "gui/$(id -u)/com.local.fleet-probe" >/dev/null 2>&1 || true
        return 0
    fi
    echo "fleet-sort: no probe timer installed; run tools/fleet-probe-install.sh" >&2
}

if [ "$needs_sort" = "1" ] && [ "$rc" = "0" ]; then
    refresh_reach
    if [ -x "$PYTHON" ] && [ -f "$REACH" ]; then
        if "$PYTHON" "$KIT/tools/catalog_sort.py" --reach "$REACH" \
               --strict-coverage >/dev/null 2>&1; then
            echo "fleet-sort: reachable-first order re-applied" >&2
            # sort writes only the catalog, so refresh the cache first
            run_real sync-cache >/dev/null 2>&1 || true
            "$PYTHON" "$KIT/tools/catalog_sort.py" --reach "$REACH" \
               --strict-coverage >/dev/null 2>&1 \
                || echo "fleet-sort: re-sort after cache failed" >&2
        else
            echo "fleet-sort: re-sort FAILED, order may be stale" >&2
        fi
    fi
fi

exit $rc

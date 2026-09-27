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
REACH="${FLEET_REACH_FILE:-$KIT/tools/fleet-reach.json}"
PYTHON="${FLEET_PYTHON:-$KIT/../runtime/.venv/bin/python}"
SELF="$(cd "$(dirname "$SRC")" && pwd)/$(basename "$SRC")"

run_real() {
    if command -v bun >/dev/null 2>&1; then
        bun "$REAL_OCX" "$@"
    else
        node "$REAL_OCX" "$@"
    fi
}

install_wrapper() {
    if [ -L "$OCX_BIN" ] || [ ! -e "$OCX_BIN" ]; then
        ln -sfn "$SELF" "$OCX_BIN.new" && mv -f "$OCX_BIN.new" "$OCX_BIN"
        echo "fleet-sort: installed -> $OCX_BIN -> $SELF"
        return 0
    fi
    echo "fleet-sort: $OCX_BIN is a real file, refusing to replace" >&2
    return 1
}

uninstall_wrapper() {
    if [ -L "$OCX_BIN" ] && [ "$(readlink "$OCX_BIN")" = "$SELF" ]; then
        ln -sfn "$REAL_OCX" "$OCX_BIN.new" && mv -f "$OCX_BIN.new" "$OCX_BIN"
        echo "fleet-sort: removed, $OCX_BIN -> $REAL_OCX"
        return 0
    fi
    echo "fleet-sort: $OCX_BIN is not our wrapper; nothing to do" >&2
    return 1
}

if [ ! -f "$REAL_OCX" ]; then
    echo "fleet-sort: real ocx not found at $REAL_OCX" >&2
    exit 127
fi

sub="${1:-}"
case "$sub" in
    --install)   install_wrapper;   exit $? ;;
    --uninstall) uninstall_wrapper; exit $? ;;
esac

run_real "$@"
rc=$?

needs_sort=0
case "$sub" in
    sync|sync-cache) needs_sort=1 ;;
esac

if [ "$needs_sort" = "1" ] && [ "$rc" = "0" ]; then
    if [ -x "$PYTHON" ] && [ -f "$REACH" ]; then
        if "$PYTHON" "$KIT/tools/catalog_sort.py" --reach "$REACH" >/dev/null 2>&1; then
            echo "fleet-sort: reachable-first order re-applied" >&2
            run_real sync-cache >/dev/null 2>&1 || true
        else
            echo "fleet-sort: re-sort FAILED, order may be stale" >&2
        fi
    fi
fi

exit $rc

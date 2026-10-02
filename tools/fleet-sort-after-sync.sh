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
# Platform abstraction: launchd / Task Scheduler / Linux supervisor.
if [ -f "$KIT/tools/platform.sh" ]; then
  # shellcheck source=platform.sh
  . "$KIT/tools/platform.sh"
fi
# Chase a symlink chain all the way down: readlink alone peels one layer,
# and a bin/ocx -> wrapper chain resolved back to this very file.
resolve_links() {
    local p="$1" nxt i=0
    while [ -L "$p" ] && [ "$i" -lt 16 ]; do
        nxt="$(readlink "$p" 2>/dev/null)" || break
        [ -n "$nxt" ] || break
        case "$nxt" in
            /*) p="$nxt" ;;
            *)  p="$(dirname "$p")/$nxt" ;;
        esac
        i=$((i + 1))
    done
    printf '%s\n' "$p"
}

# A usable real ocx is a JavaScript entry point; this wrapper never is.
# Both ocx and opencodex symlink here, so anything that resolves to this
# file is the wrapper reporting on itself.
is_real_ocx() {
    local c="$1"
    # SELF is resolved at the top of the script, before detect_real_ocx runs.
    [ -n "$c" ] || return 1
    case "$c" in
        *.mjs|*.cjs|*.js) ;;
        *) return 1 ;;
    esac
    [ -f "$c" ] || return 1
    [ "$(resolve_links "$c")" != "$(resolve_links "$SELF")" ]
}

# Resolve the real ocx entry point instead of hardcoding one machine's node
# install: FLEET_REAL_OCX wins, then whatever `ocx`/`opencodex` resolves to.
detect_real_ocx() {
  local real cand root base
  real="${FLEET_REAL_OCX:-}"
  if is_real_ocx "$real"; then
    printf '%s\n' "$(resolve_links "$real")"
    return 0
  fi

  # global node_modules roots across managers: node, homebrew, bun, nvm.
  # npm puts the package under a scope dir, hence the extra glob level.
  local roots=()
  base="$(command -v node 2>/dev/null || command -v bun 2>/dev/null || true)"
  if [ -n "$base" ]; then
    roots+=("$(dirname "$base")/../lib/node_modules")
  fi
  if command -v npm >/dev/null 2>&1; then
    roots+=("$(npm root -g 2>/dev/null || true)")
  fi
  roots+=("${NVM_DIR:-$HOME/.nvm}/versions/node"/*/lib/node_modules
          "$HOME/.bun/install/global/node_modules"
          "/usr/local/lib/node_modules"
          "/opt/homebrew/lib/node_modules"
          "/usr/lib/node_modules"
          "${LOCALAPPDATA:-/nonexistent}/Programs/nodejs/node_modules")
  for root in "${roots[@]}"; do
    [ -n "$root" ] || continue
    for cand in "$root"/*opencodex*/bin/ocx.mjs "$root"/*/*opencodex*/bin/ocx.mjs; do
      [ -f "$cand" ] || continue
      printf '%s\n' "$cand"
      return 0
    done
  done

  # last resort: the bin on PATH, fully resolved, as long as it is not us
  for real in "$(command -v opencodex 2>/dev/null || true)" \
              "$(command -v ocx 2>/dev/null || true)"; do
    [ -n "$real" ] || continue
    real="$(resolve_links "$real")"
    if is_real_ocx "$real"; then
      printf '%s\n' "$real"
      return 0
    fi
  done
  echo ""
}
# resolve our own path first: detect_real_ocx must be able to recognise and
# reject this very file when it follows the ocx/opencodex symlinks
SELF="$(cd "$(dirname "$SRC")" && pwd)/$(basename "$SRC")"
REAL_OCX="${FLEET_REAL_OCX:-$(detect_real_ocx)}"

OCX_BIN="${FLEET_OCX_BIN:-$(command -v ocx 2>/dev/null || true)}"
OCX_ALIAS="${FLEET_OCX_ALIAS:-$(command -v opencodex 2>/dev/null || true)}"
REACH="${FLEET_REACH_FILE:-$HOME/.codex/fleet-reach.json}"
PROBE="${FLEET_PROBE:-$KIT/tools/fleet_probe.py}"
REACH_MAX_AGE="${FLEET_REACH_MAX_AGE:-86400}"
ENV_FILE="${FLEET_ENV_FILE:-$KIT/../runtime/fleet.env}"
# absolute like the probe plist: launchd and relative '..' do not mix
ENV_FILE="$(cd "$(dirname "$ENV_FILE")" && pwd)/$(basename "$ENV_FILE")"
PYTHON="${FLEET_PYTHON:-}"
if [ -z "$PYTHON" ] || [ ! -x "$PYTHON" ]; then
  PYTHON="$(fleet_venv_python "$KIT/../runtime" 2>/dev/null || true)"
  if [ -z "$PYTHON" ] || [ ! -x "$PYTHON" ]; then
    PYTHON="$(command -v python3 || command -v python || echo python3)"
  fi
fi

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

if ! is_real_ocx "$REAL_OCX"; then
  echo "fleet-sort: real ocx entry point not found (tried: $REAL_OCX)" >&2
  echo "  set FLEET_REAL_OCX=/path/to/node_modules/@bitkyc08/opencodex/bin/ocx.mjs" >&2
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
    # BSD stat uses -f %m, GNU stat uses -c %Y; Windows/Git Bash has GNU stat.
    local mt
    mt=$(stat -f %m "$REACH" 2>/dev/null || stat -c %Y "$REACH" 2>/dev/null || echo 0)
    echo $(( now - mt ))
}

# refresh the snapshot when it is older than REACH_MAX_AGE
refresh_reach() {
    [ -x "$PYTHON" ] && [ -f "$PROBE" ] || return 1
    local age
    age=$(reach_age)
    [ "$age" -le "$REACH_MAX_AGE" ] && return 0
    echo "fleet-sort: snapshot is ${age}s old, re-probing in background" >&2
    # hand the work to the installed timer instead of a detached child: this
    # sandbox reaps background children on every platform
    local label="${FLEET_LABEL_PREFIX:-com.local}.fleet-probe"
    if fleet_service_exists "$label"; then
        fleet_service_start "$label" >/dev/null 2>&1 || true
        return 0
    fi
    echo "fleet-sort: no probe timer installed; run tools/fleet-probe-install.sh" >&2
}

if [ "$needs_sort" = "1" ] && [ "$rc" = "0" ]; then
    refresh_reach
    if [ -x "$PYTHON" ] && [ -f "$REACH" ]; then
        # --drop-unreachable is what removes rows for providers that have no
        # bridge at all (catalog_filter.py only judges providers with a bridge),
        # so a dead key like tokendance cannot keep 60 picker rows alive. The
        # sorter drops the flag itself when the snapshot is older than
        # REACH_MAX_AGE, so a stale probe can reorder rows but never delete.
        if "$PYTHON" "$KIT/tools/catalog_sort.py" --reach "$REACH" \
               --strict-coverage --drop-unreachable \
               --max-reach-age "$REACH_MAX_AGE" >/dev/null 2>&1; then
            echo "fleet-sort: reachable-first order re-applied" >&2
            # sort writes only the catalog, so refresh the cache first
            run_real sync-cache >/dev/null 2>&1 || true
            "$PYTHON" "$KIT/tools/catalog_sort.py" --reach "$REACH" \
               --strict-coverage --drop-unreachable \
               --max-reach-age "$REACH_MAX_AGE" >/dev/null 2>&1 \
                || echo "fleet-sort: re-sort after cache failed" >&2
        else
            echo "fleet-sort: re-sort FAILED, order may be stale" >&2
        fi
    fi
fi

exit $rc

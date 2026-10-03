#!/usr/bin/env bash
# zcode captcha pool keeper — keep one or two spendable tickets in the pool.
#
# Why this exists: the upstream spends one ticket per call, and the mint that
# produces one only passes Aliyun's traceless verification when it runs as a
# real (headed) Chrome on the warmed profile -- headless was refused 3/3
# while headed passed 3/3, measured 2026-10-02. A headed window on every call
# is exactly the "verification keeps jumping at me" this replaces, so the
# keeper runs it offscreen and only when the pool is actually short.
#
# A launchd timer (com.local.zcode-captcha-keeper) runs this every 120s; a
# short pool costs one ~5s Chrome run, a stocked pool costs one stat loop.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
POOL="${ZCODE_CAPTCHA_POOL:-$HERE/captcha_pool}"
TARGET="${ZCAP_KEEPER_TARGET:-2}"
FRESH_AGE="${ZCODE_CAPTCHA_MAX_FRESH:-600}"

case "${1:-}" in
  --status)
    # print "fresh stale pool" for the control script's status line
    fresh=0; stale=0; now="$(date +%s)"
    if [ -d "$POOL" ]; then
      for f in "$POOL"/*.txt; do
        [ -e "$f" ] || continue
        epoch="$(basename "$f" | cut -d- -f1)"
        case "$epoch" in ''|*[!0-9]*) continue ;; esac
        if [ $((now - epoch)) -le "$FRESH_AGE" ]; then fresh=$((fresh + 1)); else stale=$((stale + 1)); fi
      done
    fi
    echo "$fresh $stale $POOL"
    exit 0
    ;;
esac

fresh=0
now="$(date +%s)"
if [ -d "$POOL" ]; then
  for f in "$POOL"/*.txt; do
    [ -e "$f" ] || continue
    epoch="$(basename "$f" | cut -d- -f1)"
    case "$epoch" in ''|*[!0-9]*) continue ;; esac
    if [ $((now - epoch)) -le "$FRESH_AGE" ]; then
      fresh=$((fresh + 1))
    fi
  done
fi

if [ "$fresh" -ge "$TARGET" ]; then
  exit 0
fi

# Offscreen on purpose: the mint needs a headed Chrome to pass traceless, and
# ZCAP_ARGS puts that window outside every display. Override ZCAP_ARGS to get
# a visible window back -- which is what a deliberate warm-up wants.
pick_python() {
  # An interpreter that actually has playwright, or the mint dies on an
  # import before it tries anything (measured: bare python3 has none).
  for cand in "${ZCAP_KEEPER_PYTHON:-}" \
              "$HERE/../../../runtime/.venv/bin/python" \
              /usr/bin/python3 python3; do
    [ -n "$cand" ] || continue
    if command -v "$cand" >/dev/null 2>&1 || [ -x "$cand" ]; then
      if "$cand" -c 'import playwright' >/dev/null 2>&1; then
        printf '%s\n' "$cand"
        return 0
      fi
    fi
  done
  command -v python3
}

export ZCAP_ARGS="${ZCAP_ARGS:---window-position=20000,20000}"
exec "$(pick_python)" \
  "$HERE/captcha-mint.py" --once --pool --timeout 30

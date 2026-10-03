#!/usr/bin/env bash
# Registers the fleet bridges with opencodex (ocx) so Codex can call them
# through http://127.0.0.1:10100/v1.
set -euo pipefail

ARG_HOME=""
ARG_ENV=""
DRY_RUN=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --home)
      shift
      [ "$#" -gt 0 ] || { echo "--home requires a value" >&2; exit 1; }
      ARG_HOME="$1"
      ;;
    --home=*) ARG_HOME="${1#*=}" ;;
    --env-file)
      shift
      [ "$#" -gt 0 ] || { echo "--env-file requires a value" >&2; exit 1; }
      ARG_ENV="$1"
      ;;
    --env-file=*) ARG_ENV="${1#*=}" ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help)
      echo "Usage: setup-providers.sh [--home DIR] [--env-file PATH] [--dry-run]"
      exit 0
      ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
  shift
done

if [ -n "$ARG_ENV" ]; then
  ENVFILE="$ARG_ENV"
else
  ENVFILE="${ARG_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}/fleet.env"
fi
if [ ! -f "$ENVFILE" ]; then
  echo "fleet.env not found: ${ENVFILE} (run install.sh first)" >&2
  exit 1
fi

set -a
. "$ENVFILE"
set +a

# an explicit --home wins over whatever fleet.env recorded
if [ -n "$ARG_HOME" ]; then
  FLEET_HOME="$ARG_HOME"
fi
PORT_BASE="${PORT_BASE:-8787}"
LABEL_PREFIX="${LABEL_PREFIX:-com.local}"

if ! command -v ocx >/dev/null 2>&1; then
  echo "ocx not found; install it with: npm install -g @bitkyc08/opencodex" >&2
  exit 1
fi

# Mask every --api-key argument. A dry run echoes its plan, and that plan once
# carried the fleet's real keys into terminal scrollback and screenshots; the
# plan does not need the secret to prove what it would run.
mask_args() {
  local out="" arg skip=0
  for arg in "$@"; do
    if [ "$skip" = "1" ]; then
      out="$out ***"
      skip=0
    else
      case "$arg" in
        --api-key) out="$out $arg"; skip=1 ;;
        --api-key=*) out="$out --api-key=***" ;;
        *) out="$out $arg" ;;
      esac
    fi
  done
  printf '%s' "${out# }"
}

run() {
  if [ "$DRY_RUN" = "1" ]; then
    echo "  [dry-run] $(mask_args "$@")"
  else
    "$@" || echo "  [warn] command failed: $(mask_args "$@")" >&2
  fi
}

# name | port-offset | key-env
PROVIDERS=(
  "workbuddy|0|CODEBUDDY2OPENAI_KEY"
  "workbuddy-gpt|1|CODEBUDDY2OPENAI_KEY"
  "qoder|2|QODER2CODEX_KEY"
  "codely|3|CODELY2CODEX_KEY"
  "trae|4|TRAE2CODEX_KEY"
  "lingxi|5|LINGXI2CODEX_KEY"
  "xhx|6|XHX2CODEX_KEY"
  "gemini|7|GEMINI2CODEX_KEY"
  "catpaw|8|CATPAW2CODEX_KEY"
  "antigravity|10|ANTIGRAVITY2CODEX_KEY"
  "qwen|11|QWEN2CODEX_KEY"
  "cline|12|CLINE2CODEX_KEY"
  "zcode|13|ZCODE2CODEX_KEY"
  "kimi-code|15|KIMI2CODEX_KEY"
  "minimax|16|MINIMAX2CODEX_KEY"
)

echo "opencodex provider setup (port base ${PORT_BASE})"
run ocx service
# The launchd service above starts the proxy at boot, but `codex` can still be
# launched before that job has run; `ocx status` then answers "Restart safety:
# AT RISK after restart (custom local gateway lifecycle is not managed by
# opencodex)" together with "Codex autostart shim is not installed". The shim
# makes the codex binary ensure the proxy itself on launch and closes that gap;
# it is reversible with `ocx codex-shim uninstall`.
# The shim wraps the first `codex` on PATH. Desktop Codex puts its own bundled
# binary on that PATH (.../ChatGPT.app/Contents/Resources/codex); wrapping that
# would rename a file inside a running app bundle, so refuse and say why. The
# launchd service above still autostarts the proxy in that case.
codex_on_path="$(command -v codex || true)"
case "$codex_on_path" in
  *".app/Contents/Resources/"*)
    echo "  (skipping codex-shim: first codex on PATH is ${codex_on_path},"
    echo "   inside an app bundle; 'ocx service' still autostarts the proxy)"
    ;;
  *) run ocx codex-shim install ;;
esac

# Read one KEY out of any installed service definition, on any platform.
# macOS keeps launchd plists, Windows/Linux keep the generated wrapper; both are
# parsed by tools/fleet_platform.py so this works without PlistBuddy.
service_key_for() {
  local label="$1" keyenv="$2"
  local py tools
  py="$(command -v python3 || command -v python || echo python3)"
  tools="${FLEET_HOME:-.}/tools"
  [ -d "$tools" ] || tools="$(cd "$(dirname "${BASH_SOURCE[0]}")/../tools" && pwd)"
  FLEET_SERVICE_DIR="${FLEET_SERVICE_DIR:-}" "$py" - "$label" "$keyenv" <<PYKEY
import os, sys
sys.path.insert(0, "$tools")
try:
    from fleet_platform import service_key
except Exception:
    sys.exit(0)
sys.stdout.write(service_key(sys.argv[1], sys.argv[2]))
PYKEY
}


for row in "${PROVIDERS[@]}"; do
  name="$(echo "$row" | cut -d'|' -f1)"
  offset="$(echo "$row" | cut -d'|' -f2)"
  keyenv="$(echo "$row" | cut -d'|' -f3)"
  port=$((PORT_BASE + offset))
  key="${!keyenv:-}"
  if [ -z "$key" ]; then
    # Live fleets keep keys in the installed service definition (launchd plist on
    # macOS, the generated wrapper elsewhere) while fleet.env may be absent.
    # One python helper reads all three backends; PlistBuddy is macOS-only.
    for label in "${LABEL_PREFIX}.${name}2codex" "${LABEL_PREFIX}.workbuddy2codex-gpt"; do
      got="$(service_key_for "$label" "$keyenv")"
      if [ -n "$got" ]; then key="$got"; break; fi
    done
  fi
  # "local" only works for bridges that do not enforce a key. A bridge that does
  # (qwen) would come back as 401 on every request, so it stays unregistered until
  # its real key is in fleet.env; tools/catalog_filter.py hides its picker rows.
  if [ -z "$key" ]; then
    if grep -qs "BRIDGE_KEY = os.environ" "${FLEET_HOME:-.}/bridges/${name}/${name}_bridge.py" 2>/dev/null; then
      echo "  (skipping ${name}: ${keyenv} is not set anywhere)" >&2
      continue
    fi
    key="local"
  fi
  run ocx provider add "$name" --adapter openai-chat --base-url "http://127.0.0.1:${port}/v1" --api-key "$key" --allow-private-network --force
  run ocx models provider "$name" on
done

if [ -n "${TOKENDANCE_API_KEY:-}" ]; then
  run ocx provider add tokendance --adapter openai-chat --base-url "https://tokendance.space/gateway/v1" --api-key "${TOKENDANCE_API_KEY}" --allow-private-network --force
  run ocx models provider tokendance on
  # NOTE: an explicit --set narrows the provider to just those ids and ocx sync then
  # drops every other live model from the Codex catalog. tokendance must stay on
  # "all models" (95 entries), so clear the selection instead of pinning one id.
  # The default model is pinned in ~/.codex/config.toml at the end of this script.
  run ocx models selected tokendance --clear
else
  echo "  (TOKENDANCE_API_KEY not set; skipping tokendance)"
fi

run ocx sync

# StepFun Plan API (official paid plan; step-5-preview has a 1M context window).
# A local proxy tool can resolve api.stepfun.com to a fake-ip non-global address,
# so registration needs --allow-private-network or the destination policy blocks
# model discovery.
STEPFUN_REGISTERED=0

if [ -n "${STEPFUN_PLAN_API_KEY:-}" ]; then
  # Through the image-cap shim (127.0.0.1:15722 -> api.stepfun.com/step_plan/v1),
  # not straight at StepFun: Codex re-sends its whole history every turn and the
  # Plan API refuses the 71st image (README "stepfun 系走 image-cap shim 15722").
  # The shim forwards the key byte for byte, so this registration answers
  # exactly like the CC Switch route does.
  run ocx provider add stepfun --adapter openai-chat --base-url http://127.0.0.1:15722/v1 --api-key "${STEPFUN_PLAN_API_KEY}" --allow-private-network --force
  # bare `ocx models` refreshes the discovery cache; without it
  # `ocx models provider stepfun on` reports "no models are available".
  run ocx models >/dev/null
  run ocx models provider stepfun on
  run ocx models selected stepfun --set step-5-preview,step-3.7-flash,step-3.5-flash-2603,step-3.5-flash,step-router-v1
  STEPFUN_REGISTERED=1
else
  echo "  (STEPFUN_PLAN_API_KEY not set; skipping stepfun plan api)"
fi

# `ocx provider add --force` wipes alias/modelAliases on every provider, which
# puts the long names back in the Codex picker. Re-register the short names
# after all provider adds; short_aliases.py re-runs `ocx sync` itself.
KIT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ "$DRY_RUN" = "1" ]; then
  run python3 "$KIT/tools/short_aliases.py" --dry-run
elif [ -f "$KIT/tools/short_aliases.py" ]; then
  run python3 "$KIT/tools/short_aliases.py"
else
  echo "  [warn] $KIT/tools/short_aliases.py missing; picker names stay long" >&2
fi

# `model = ...` in ~/.codex/config.toml decides which model the Codex picker opens on.
# CC Switch owns that file and `ocx provider add --force` rewrites it, so re-pin the
# default after the last sync. fleet.env can override with FLEET_DEFAULT_MODEL.
CODEX_TOML="${HOME}/.codex/config.toml"
DEFAULT_MODEL="${FLEET_DEFAULT_MODEL:-}"
if [ -z "$DEFAULT_MODEL" ] && [ "$STEPFUN_REGISTERED" != "1" ]; then
  # Neither an operator override nor a registered stepfun provider. The fallback
  # below names a provider this same script just skipped, and pinning it would
  # leave Codex opening on a provider that is not registered.
  echo "  [warn] no FLEET_DEFAULT_MODEL and stepfun was not registered" >&2
  echo "         (STEPFUN_PLAN_API_KEY unset); leaving the existing default alone" >&2
elif [ -z "$DEFAULT_MODEL" ]; then
  DEFAULT_MODEL="stepfun/step-5-preview"
fi
if [ -n "$DEFAULT_MODEL" ] && [ -f "$CODEX_TOML" ]; then
  if [ "$DRY_RUN" = "1" ]; then
    echo "  [dry-run] pin ${DEFAULT_MODEL} as the default model in ${CODEX_TOML}"
  else
    python3 - "$CODEX_TOML" "$DEFAULT_MODEL" <<'PIN' || echo "  [warn] default model pin failed" >&2
import sys

path, model = sys.argv[1], sys.argv[2]
with open(path, encoding="utf-8") as fh:
    lines = fh.readlines()
out, pinned = [], False
for line in lines:
    if line.startswith("model = ") or line.startswith("model="):
        out.append('model = "%s"\n' % model)
        pinned = True
    else:
        out.append(line)
if not pinned:
    for i, line in enumerate(out):
        if line.startswith("model_provider"):
            out.insert(i + 1, 'model = "%s"\n' % model)
            break
with open(path, "w", encoding="utf-8") as fh:
    fh.writelines(out)
print("  pinned default model: %s" % model)
PIN
  fi
elif [ ! -f "$CODEX_TOML" ]; then
  echo "  [warn] ${CODEX_TOML} not found; default model not pinned" >&2
fi

# The pinned default must still answer a real request. This fleet once
# re-pinned trae/trae-step-5-preview on every setup after that route died
# (401 -> 502): the gating above refuses an unregistered provider, but no
# one checked the pinned route's health. The guard probes the live default
# on its own route and jumps it back to the stepfun harbor when dead.
GUARD="${KIT}/tools/default_model_guard.py"
if [ -f "$GUARD" ]; then
  if [ "$DRY_RUN" = "1" ]; then
    run python3 "$GUARD" --dry-run --env-file "$ENVFILE"
  else
    run python3 "$GUARD" --env-file "$ENVFILE" \
      || echo "  [warn] default-model guard reported a problem" >&2
  fi
else
  echo "  [warn] $GUARD missing; the default model is not health-guarded" >&2
fi


# StepFun's Plan API answers a request with 70 images and refuses the 71st with
# 400 images_too_many, and Codex re-sends its whole history every turn, so a
# session that pastes screenshots eventually crosses that ceiling no matter what
# the operator does (tools/image_cap.py holds the measurement). The image-cap
# shim de-duplicates and caps photos on 15722.
#
# It goes *after* CC Switch, never instead of it, and this script deliberately
# does not run tools/pin_shim_base_url.py any more. Measured 2026-10-01: Codex
# sends `Authorization: Bearer PROXY_MANAGED` (see experimental_bearer_token in
# config.toml) and only CC Switch holds the real StepFun key, so a provider whose
# base_url points straight at the shim forwards that placeholder verbatim and
# every turn answers 401 "Incorrect API key provided" -- the pin turned the shim
# into a hop that broke authentication. Pointing Codex at CC Switch and letting
# CC Switch's routing table forward to the shim keeps both halves working:
# 15721 with the placeholder token answers 200 and the shim's own request counter
# goes up, so the cap is still applied. tools/pin_cc_switch_endpoint.py holds that
# re-point, and the shim service re-runs it on a timer (IMAGE_CAP_CC_PIN_INTERVAL)
# because CC Switch resets the row whenever the operator switches providers.
SHIM_SH="$KIT/tools/stepfun_image_shim.sh"
if [ -n "${FLEET_HOME:-}" ] && [ -f "${FLEET_HOME}/tools/stepfun_image_shim.sh" ]; then
  # prefer the deployed copy: its launchd job then runs the same file a
  # re-install refreshes, not this git checkout
  SHIM_SH="${FLEET_HOME}/tools/stepfun_image_shim.sh"
fi
if [ "$DRY_RUN" = "1" ]; then
  echo "  [dry-run] bash ${SHIM_SH} install-timer"
else
  bash "$SHIM_SH" install-timer || echo "  [warn] stepfun image-cap shim install failed" >&2
fi

run ocx service restart
echo "done. inspect with: ocx models live"

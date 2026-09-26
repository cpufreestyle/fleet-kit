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

run() {
  if [ "$DRY_RUN" = "1" ]; then
    echo "  [dry-run] $*"
  else
    "$@" || echo "  [warn] command failed: $*" >&2
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
)

echo "opencodex provider setup (port base ${PORT_BASE})"
run ocx service

for row in "${PROVIDERS[@]}"; do
  name="$(echo "$row" | cut -d'|' -f1)"
  offset="$(echo "$row" | cut -d'|' -f2)"
  keyenv="$(echo "$row" | cut -d'|' -f3)"
  port=$((PORT_BASE + offset))
  key="${!keyenv:-}"
  if [ -z "$key" ]; then
    # live fleets keep keys in the launchd plists while fleet.env may be absent;
    # same source tools/fleet_chat_test.py reads.
    for pl in "$HOME/Library/LaunchAgents/${LABEL_PREFIX}.${name}2codex.plist" "$HOME/Library/LaunchAgents/${LABEL_PREFIX}.workbuddy2codex-gpt.plist"; do
      [ -f "$pl" ] || continue
      got="$(/usr/libexec/PlistBuddy -c "Print :EnvironmentVariables:${keyenv}" "$pl" 2>/dev/null || true)"
      if [ -n "$got" ]; then key="$got"; break; fi
    done
  fi
  [ -z "$key" ] && key="local"   # bridges that do not enforce a key tolerate this
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
if [ -n "${STEPFUN_PLAN_API_KEY:-}" ]; then
  run ocx provider add stepfun --adapter openai-chat --base-url https://api.stepfun.com/step_plan/v1 --api-key "${STEPFUN_PLAN_API_KEY}" --allow-private-network --force
  # bare `ocx models` refreshes the discovery cache; without it
  # `ocx models provider stepfun on` reports "no models are available".
  run ocx models >/dev/null
  run ocx models provider stepfun on
  run ocx models selected stepfun --set step-5-preview,step-3.7-flash,step-3.5-flash-2603,step-3.5-flash,step-router-v1
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
DEFAULT_MODEL="${FLEET_DEFAULT_MODEL:-stepfun/step-5-preview}"
if [ -f "$CODEX_TOML" ]; then
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
else
  echo "  [warn] ${CODEX_TOML} not found; default model not pinned" >&2
fi

run ocx service restart
echo "done. inspect with: ocx models live"

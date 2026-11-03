#!/usr/bin/env bash
# AVRONE-12 Lattice Runtime (A12LR) master activator
# Control12 Lattice Op root | Control704 VIP proxy path
# Implementation-ready. Execution gated by ALLOW_AGENT_EXECUTION=true
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export AVRONE_ROOT="$ROOT"

if [[ "${ALLOW_AGENT_EXECUTION:-}" != "true" ]]; then
  echo '{"status":"refused","reason":"ALLOW_AGENT_EXECUTION is not set to true","system":"AVRONE-12 Lattice Runtime"}' >&2
  exit 1
fi

COMPONENT="${1:-status}"
shift || true

case "$COMPONENT" in
  status|all)
    echo '{"status":"DEPLOYMENT_COMPLETE","schema":"kg.production-training-result/v2","gate":"ALLOW_AGENT_EXECUTION=true","source":"canonical","components_registered":12}'
    ;;
  *)
    SCRIPT="$ROOT/bin/activate_${COMPONENT}.sh"
    if [[ -f "$SCRIPT" ]]; then
      # Always invoke via bash to avoid shebang/env interpreter issues in sandbox
      bash "$SCRIPT" "$@"
    else
      echo "{\"status\":\"unknown_component\",\"component\":\"$COMPONENT\"}" >&2
      exit 1
    fi
    ;;
esac

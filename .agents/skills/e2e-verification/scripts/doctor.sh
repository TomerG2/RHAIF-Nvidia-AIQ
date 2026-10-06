#!/usr/bin/env bash
# Read-only readiness check: is this AI-Q instance worth driving?
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export AIQ_SERVER_URL="${AIQ_SERVER_URL:-http://127.0.0.1:8000}"

echo "Doctor target: ${AIQ_SERVER_URL}" >&2

health_json="$(python3 "${SKILL_DIR}/scripts/aiq.py" health)"
echo "${health_json}"

agents_json="$(python3 "${SKILL_DIR}/scripts/aiq.py" agents)"
echo "${agents_json}"

python3 - "${agents_json}" <<'PY'
import json, sys
payload = json.loads(sys.argv[1])
agents = payload.get("agents") or []
types = {a.get("agent_type") for a in agents if isinstance(a, dict)}
needed = {"shallow_researcher", "deep_researcher"}
missing = sorted(needed - types)
if missing:
    print(f"FAIL: missing agent types: {', '.join(missing)}", file=sys.stderr)
    sys.exit(1)
print("OK: shallow_researcher and deep_researcher are listed", file=sys.stderr)
PY

# Optional cluster-side signals when oc is available and logged in.
if command -v oc >/dev/null 2>&1; then
  if oc -n aiq-inference get inferenceservice vllm-inference-service >/dev/null 2>&1; then
    ready="$(oc -n aiq-inference get inferenceservice vllm-inference-service \
      -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || true)"
    echo "InferenceService Ready=${ready:-unknown}" >&2
    if [[ "${ready}" != "True" ]]; then
      echo "WARN: in-cluster vLLM is not Ready; shallow research will likely fail." >&2
    fi
  else
    echo "NOTE: InferenceService not found (CRD missing or not deployed yet)." >&2
  fi
fi

echo "Doctor passed." >&2

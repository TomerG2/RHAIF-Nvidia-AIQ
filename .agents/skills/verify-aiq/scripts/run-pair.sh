#!/usr/bin/env bash
# Drive shallow_researcher then deep_researcher with the same question.
# Writes evidence under artifacts/<run-id>/; does not delete it on exit.
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export AIQ_SERVER_URL="${AIQ_SERVER_URL:-http://127.0.0.1:8000}"

QUESTION="${VERIFY_AIQ_QUESTION:-What is the capital of France?}"
RUN_ID="${VERIFY_AIQ_RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
OUT_DIR="${VERIFY_AIQ_ARTIFACT_DIR:-${SKILL_DIR}/artifacts/${RUN_ID}}"
export VERIFY_AIQ_STATE_DIR="${VERIFY_AIQ_STATE_DIR:-/tmp/verify-aiq-${RUN_ID}}"
JOBS_FILE="${VERIFY_AIQ_STATE_DIR}/started-jobs.txt"

mkdir -p "${OUT_DIR}" "${VERIFY_AIQ_STATE_DIR}"
: >"${JOBS_FILE}"

echo "${QUESTION}" >"${OUT_DIR}/question.txt"
{
  echo "AIQ_SERVER_URL=${AIQ_SERVER_URL}"
  echo "run_id=${RUN_ID}"
  echo "VERIFY_AIQ_STATE_DIR=${VERIFY_AIQ_STATE_DIR}"
} >"${OUT_DIR}/env.txt"

run_one() {
  local agent_type="$1"
  local label="$2"
  local submit_out report_out
  submit_out="${OUT_DIR}/${label}-submit.json"
  report_out="${OUT_DIR}/${label}-report.json"

  echo "=== ${label}: submitting ${agent_type} ===" >&2
  python3 "${SKILL_DIR}/scripts/aiq.py" submit "${QUESTION}" "${agent_type}" \
    | tee "${submit_out}"

  local job_id
  job_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["job_id"])' <"${submit_out}")"
  echo "${job_id}" >>"${JOBS_FILE}"
  echo "${job_id}" >"${OUT_DIR}/${label}-job-id.txt"

  echo "=== ${label}: polling ${job_id} ===" >&2
  python3 "${SKILL_DIR}/scripts/aiq.py" research_poll "${job_id}" \
    | tee "${report_out}"
}

run_one "shallow_researcher" "shallow"
run_one "deep_researcher" "deep"

cat >"${OUT_DIR}/verdict-template.md" <<'EOF'
# Reasonableness verdict

Fill this after reading `shallow-report.json` and `deep-report.json`.

## Shallow
- Verdict: makes sense | does not make sense
- One-line why:

## Deep
- Verdict: makes sense | does not make sense
- One-line why:
EOF

echo "Evidence written to ${OUT_DIR}" >&2
echo "${OUT_DIR}"

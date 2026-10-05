#!/usr/bin/env bash
# Tear down only resources this verification run started.
# Never deletes evidence under artifacts/.
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="${VERIFY_AIQ_STATE_DIR:-}"

if [[ -z "${STATE_DIR}" ]]; then
  echo "VERIFY_AIQ_STATE_DIR unset; nothing to clean." >&2
  exit 0
fi

export AIQ_SERVER_URL="${AIQ_SERVER_URL:-http://127.0.0.1:8000}"
JOBS_FILE="${STATE_DIR}/started-jobs.txt"
PID_FILE="${STATE_DIR}/port-forward.pid"

if [[ -f "${JOBS_FILE}" ]]; then
  while read -r job_id; do
    [[ -z "${job_id}" ]] && continue
    echo "Cancelling job ${job_id} (best-effort)..." >&2
    python3 "${SKILL_DIR}/scripts/aiq.py" cancel "${job_id}" >/dev/null 2>&1 || true
  done <"${JOBS_FILE}"
fi

if [[ -f "${PID_FILE}" ]]; then
  pf_pid="$(cat "${PID_FILE}")"
  if kill -0 "${pf_pid}" 2>/dev/null; then
    echo "Stopping port-forward pid ${pf_pid}" >&2
    kill "${pf_pid}" 2>/dev/null || true
    wait "${pf_pid}" 2>/dev/null || true
  fi
  rm -f "${PID_FILE}"
fi

echo "Cleanup done. Evidence under ${SKILL_DIR}/artifacts/ is retained." >&2

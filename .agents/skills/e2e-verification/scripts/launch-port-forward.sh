#!/usr/bin/env bash
# Verification scaffolding: starts an oc port-forward to aiq-backend and writes
# the PID so cleanup can tear down only what this run started.
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="${VERIFY_AIQ_STATE_DIR:-/tmp/verify-aiq-$$}"
NAMESPACE="${VERIFY_AIQ_NAMESPACE:-aiq}"
LOCAL_PORT="${VERIFY_AIQ_LOCAL_PORT:-8000}"
SERVICE="${VERIFY_AIQ_SERVICE:-aiq-backend}"
REMOTE_PORT="${VERIFY_AIQ_REMOTE_PORT:-8000}"
READY_TIMEOUT_SECS="${VERIFY_AIQ_READY_TIMEOUT_SECS:-120}"

mkdir -p "${STATE_DIR}"
PID_FILE="${STATE_DIR}/port-forward.pid"
LOG_FILE="${STATE_DIR}/port-forward.log"
URL_FILE="${STATE_DIR}/aiq-server-url"

if [[ -f "${PID_FILE}" ]]; then
  old_pid="$(cat "${PID_FILE}")"
  if kill -0 "${old_pid}" 2>/dev/null; then
    echo "Port-forward already running (pid ${old_pid}). Reusing ${STATE_DIR}." >&2
    echo "export AIQ_SERVER_URL=$(cat "${URL_FILE}")"
    echo "export VERIFY_AIQ_STATE_DIR=${STATE_DIR}"
    exit 0
  fi
fi

if ! command -v oc >/dev/null 2>&1; then
  echo "oc is required" >&2
  exit 1
fi

if ! oc -n "${NAMESPACE}" get "svc/${SERVICE}" >/dev/null 2>&1; then
  echo "Service ${SERVICE} not found in namespace ${NAMESPACE}." >&2
  echo "Deploy the pattern first (./pattern.sh make install), then retry." >&2
  exit 1
fi

oc -n "${NAMESPACE}" port-forward "svc/${SERVICE}" "${LOCAL_PORT}:${REMOTE_PORT}" \
  >"${LOG_FILE}" 2>&1 &
pf_pid=$!
echo "${pf_pid}" >"${PID_FILE}"
echo "http://127.0.0.1:${LOCAL_PORT}" >"${URL_FILE}"

cleanup_on_fail() {
  if kill -0 "${pf_pid}" 2>/dev/null; then
    kill "${pf_pid}" 2>/dev/null || true
    wait "${pf_pid}" 2>/dev/null || true
  fi
  rm -f "${PID_FILE}"
}
trap cleanup_on_fail ERR

deadline=$((SECONDS + READY_TIMEOUT_SECS))
export AIQ_SERVER_URL="http://127.0.0.1:${LOCAL_PORT}"
while (( SECONDS < deadline )); do
  if ! kill -0 "${pf_pid}" 2>/dev/null; then
    echo "Port-forward exited early. Log:" >&2
    cat "${LOG_FILE}" >&2 || true
    exit 1
  fi
  if python3 "${SKILL_DIR}/scripts/aiq.py" health >/dev/null 2>&1; then
    trap - ERR
    echo "Port-forward ready (pid ${pf_pid})." >&2
    echo "export AIQ_SERVER_URL=${AIQ_SERVER_URL}"
    echo "export VERIFY_AIQ_STATE_DIR=${STATE_DIR}"
    exit 0
  fi
  sleep 2
done

echo "Timed out waiting for AI-Q health on ${AIQ_SERVER_URL}" >&2
cleanup_on_fail
exit 1

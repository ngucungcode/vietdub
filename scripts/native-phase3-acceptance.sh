#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/native-common.sh"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Nghiem thu native Phase 3 phai chay bang root" >&2
  exit 2
fi

MODEL_ID="${1:-${DUB_DEFAULT_TRANSLATION_MODEL_ID}}"
REPORT_PATH="${DUB_NATIVE_ROOT}/state/phase3-acceptance.json"
SUPERVISORCTL="${DUB_VENV_DIR}/bin/supervisorctl"
SUPERVISOR_CONFIG="${PROJECT_ROOT}/native/supervisord.conf"
WORKER_WAS_RUNNING=0

restore_worker() {
  if [[ "${WORKER_WAS_RUNNING}" -eq 1 ]]; then
    "${SUPERVISORCTL}" -c "${SUPERVISOR_CONFIG}" start worker >/dev/null || true
  fi
}
trap restore_worker EXIT

if "${SUPERVISORCTL}" -c "${SUPERVISOR_CONFIG}" status worker 2>/dev/null \
    | grep -q ' RUNNING '; then
  WORKER_WAS_RUNNING=1
  "${SUPERVISORCTL}" -c "${SUPERVISOR_CONFIG}" stop worker >/dev/null
fi

runuser -u "${DUB_NATIVE_USER}" -- env \
  HOME="${DUB_NATIVE_ROOT}/home" \
  DUB_DATABASE_PATH="${DUB_DATABASE_PATH}" \
  DUB_MODELS_LOCK_PATH="${DUB_MODELS_LOCK_PATH}" \
  DUB_MODELS_DIR="${DUB_MODELS_DIR}" \
  DUB_LLAMA_SERVER_BINARY="${DUB_LLAMA_SERVER_BINARY}" \
  DUB_LLAMA_SERVER_PORT="${DUB_LLAMA_SERVER_PORT}" \
  DUB_LLAMA_CONTEXT_SIZE="${DUB_LLAMA_CONTEXT_SIZE}" \
  DUB_LLAMA_MAX_OUTPUT_TOKENS="${DUB_LLAMA_MAX_OUTPUT_TOKENS}" \
  DUB_LLAMA_STARTUP_TIMEOUT_SECONDS="${DUB_LLAMA_STARTUP_TIMEOUT_SECONDS}" \
  DUB_LLAMA_REQUEST_TIMEOUT_SECONDS="${DUB_LLAMA_REQUEST_TIMEOUT_SECONDS}" \
  "${DUB_VENV_DIR}/bin/python" "${SCRIPT_DIR}/native-phase3-acceptance.py" \
  --model-id "${MODEL_ID}" --report "${REPORT_PATH}"

echo "Bao cao Phase 3: ${REPORT_PATH}"

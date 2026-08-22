#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/native-common.sh"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Nghiệm thu native Phase 2 phải chạy bằng root" >&2
  exit 2
fi

MODEL_ID="${1:-${DUB_DEFAULT_ASR_MODEL_ID}}"
if [[ ! "${MODEL_ID}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
  echo "ID model ASR không hợp lệ" >&2
  exit 2
fi

SUPERVISORCTL="${DUB_VENV_DIR}/bin/supervisorctl"
SUPERVISOR_CONFIG="${PROJECT_ROOT}/native/supervisord.conf"
REPORT_PATH="${DUB_NATIVE_ROOT}/state/phase2-acceptance-${MODEL_ID}.json"
WORKER_WAS_RUNNING=false

restore_worker() {
  if [[ "${WORKER_WAS_RUNNING}" == true ]]; then
    "${SUPERVISORCTL}" -c "${SUPERVISOR_CONFIG}" start worker >/dev/null || true
  fi
}
trap restore_worker EXIT

if "${SUPERVISORCTL}" -c "${SUPERVISOR_CONFIG}" status worker 2>/dev/null \
  | grep -q ' RUNNING '; then
  WORKER_WAS_RUNNING=true
  "${SUPERVISORCTL}" -c "${SUPERVISOR_CONFIG}" stop worker
fi

runuser -u "${DUB_NATIVE_USER}" --preserve-environment -- \
  env \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_DATASETS_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
  "${DUB_VENV_DIR}/bin/python" \
  "${PROJECT_ROOT}/scripts/native-phase2-acceptance.py" \
  --lock "${DUB_MODELS_LOCK_PATH}" \
  --models-dir "${DUB_MODELS_DIR}" \
  --model-id "${MODEL_ID}" \
  --compute-type "${DUB_ASR_COMPUTE_TYPE}" \
  --report "${REPORT_PATH}"

echo "Báo cáo Phase 2: ${REPORT_PATH}"

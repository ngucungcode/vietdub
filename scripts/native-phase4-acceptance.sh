#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/native-common.sh"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Nghiệm thu native Phase 4 phải chạy bằng root" >&2
  exit 2
fi

REPORT_PATH="${DUB_NATIVE_ROOT}/state/phase4-acceptance.json"
ARTIFACT_ROOT="${DUB_NATIVE_ROOT}/state/phase4-acceptance-artifacts"
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

install -d -m 0750 -o "${DUB_NATIVE_USER}" -g "${DUB_NATIVE_USER}" \
  "$(dirname -- "${REPORT_PATH}")" "${ARTIFACT_ROOT}"

EXTRA_ARGUMENTS=("$@")
if [[ "${#EXTRA_ARGUMENTS[@]}" -eq 0 ]]; then
  EXTRA_ARGUMENTS=(--quick)
fi

runuser -u "${DUB_NATIVE_USER}" -- env \
  HOME="${DUB_NATIVE_ROOT}/home" \
  PYTHONPATH="${PYTHONPATH}" \
  HF_HUB_OFFLINE=1 \
  TRANSFORMERS_OFFLINE=1 \
  HF_DATASETS_OFFLINE=1 \
  HF_HUB_DISABLE_TELEMETRY=1 \
  WANDB_DISABLED=true \
  WANDB_MODE=offline \
  VIENEU_CODEC_PATH="${VIENEU_CODEC_PATH}" \
  DUB_FFMPEG_BINARY=ffmpeg \
  "${DUB_VENV_DIR}/bin/python" "${SCRIPT_DIR}/phase4_acceptance.py" \
  --lock "${DUB_MODELS_LOCK_PATH}" \
  --models-dir "${DUB_MODELS_DIR}" \
  --report "${REPORT_PATH}" \
  --artifact-root "${ARTIFACT_ROOT}" \
  --separation-model-id "${DUB_DEFAULT_SEPARATION_MODEL_ID}" \
  --tts-model-id "${DUB_DEFAULT_TTS_MODEL_ID}" \
  --tts-support-model-id "${DUB_TTS_SUPPORT_MODEL_ID}" \
  --tiger-source-dir "${DUB_TIGER_SOURCE_DIR}" \
  --vieneu-entrypoint "${DUB_VIENEU_ENTRYPOINT}" \
  "${EXTRA_ARGUMENTS[@]}"

echo "Báo cáo Phase 4: ${REPORT_PATH}"

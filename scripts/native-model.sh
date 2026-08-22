#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/native-common.sh"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Quan ly model native phai chay bang root" >&2
  exit 2
fi
if [[ "$#" -lt 1 ]]; then
  echo "Cach dung: $0 {list|install MODEL_ID|verify [MODEL_ID]}" >&2
  exit 2
fi

SERVICE_HOME="${DUB_NATIVE_ROOT}/home"
install -d -m 0750 -o "${DUB_NATIVE_USER}" -g "${DUB_NATIVE_USER}" \
  "${SERVICE_HOME}" "${SERVICE_HOME}/.cache" "${DUB_MODELS_DIR}"

exec runuser -u "${DUB_NATIVE_USER}" -- env \
  HOME="${SERVICE_HOME}" \
  HF_HOME="${SERVICE_HOME}/.cache/huggingface" \
  DUB_MODELS_LOCK_PATH="${DUB_MODELS_LOCK_PATH}" \
  DUB_MODELS_DIR="${DUB_MODELS_DIR}" \
  "${DUB_VENV_DIR}/bin/python" -m dub_server.model_manager \
  "$@" --lock "${DUB_MODELS_LOCK_PATH}" --models-dir "${DUB_MODELS_DIR}"

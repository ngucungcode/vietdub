#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/native-common.sh"

required_commands=(ffmpeg ffprobe nvidia-smi qbittorrent-nox sqlite3)
for command_name in "${required_commands[@]}"; do
  command -v "${command_name}" >/dev/null
done

[[ -x "${DUB_PROWLARR_BIN}" ]]
[[ -x "${DUB_VENV_DIR}/bin/python" ]]

for writable_dir in \
  "${DUB_DATABASE_PATH%/*}" \
  "${DUB_MODELS_DIR}" \
  "${DUB_INCOMING_DIR}" \
  "${DUB_JOBS_DIR}" \
  "${DUB_OUTPUT_DIR}"; do
  runuser -u "${DUB_NATIVE_USER}" -- test -w "${writable_dir}"
done

for secret_path in \
  "${DUB_PROWLARR_API_KEY_FILE}" \
  "${DUB_QBITTORRENT_PASSWORD_FILE}" \
  "${DUB_OPENSUBTITLES_API_KEY_FILE}" \
  "${DUB_OPENSUBTITLES_TOKEN_FILE}" \
  "${DUB_OPENSUBTITLES_BASE_URL_FILE}"; do
  if [[ -e "${secret_path}" ]]; then
    mode="$(stat -c '%a' "${secret_path}")"
    if [[ "${mode}" != "600" && "${mode}" != "400" ]]; then
      echo "Quyền file secret không an toàn: ${secret_path} (${mode})" >&2
      exit 1
    fi
  fi
done

runuser -u "${DUB_NATIVE_USER}" --preserve-environment -- \
  "${DUB_VENV_DIR}/bin/python" -m dub_server.worker --once

echo "Preflight native đạt"

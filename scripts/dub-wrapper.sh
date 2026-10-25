#!/usr/bin/env bash

set -Eeuo pipefail

WRAPPER_PATH="$(readlink -f -- "${BASH_SOURCE[0]}")"
PROJECT_ROOT="$(CDPATH= cd -- "$(dirname -- "${WRAPPER_PATH}")/.." && pwd)"
export DUB_PROJECT_ROOT="${PROJECT_ROOT}"

source "${PROJECT_ROOT}/scripts/native-common.sh"

if [[ ! -x "${DUB_VENV_DIR}/bin/dub" ]]; then
  echo "CLI chưa được cài; hãy chạy ${PROJECT_ROOT}/install.sh" >&2
  exit 2
fi

exec "${DUB_VENV_DIR}/bin/dub" "$@"

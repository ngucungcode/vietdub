#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(CDPATH= cd -- "${SCRIPT_DIR}/.." && pwd)"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Bootstrap native phải chạy bằng root" >&2
  exit 2
fi

if [[ ! -f "${PROJECT_ROOT}/.env.native" ]]; then
  cp "${PROJECT_ROOT}/.env.native.example" "${PROJECT_ROOT}/.env.native"
  chmod 600 "${PROJECT_ROOT}/.env.native"
fi

source "${SCRIPT_DIR}/native-common.sh"

PYTHON_BIN="${PYTHON_BIN:-python3}"
INSTALL_TEST_MODE="${DUB_INSTALL_TEST_MODE:-smoke}"
case "${INSTALL_TEST_MODE}" in
  smoke|full) ;;
  *)
    echo "DUB_INSTALL_TEST_MODE phải là smoke hoặc full" >&2
    exit 2
    ;;
esac

if [[ -z "${DUB_BUILD_JOBS:-}" ]]; then
  detected_build_jobs="$(nproc)"
  if ((detected_build_jobs > 16)); then
    detected_build_jobs=16
  fi
  export DUB_BUILD_JOBS="${detected_build_jobs}"
elif [[ ! "${DUB_BUILD_JOBS}" =~ ^[1-9][0-9]*$ ]]; then
  echo "DUB_BUILD_JOBS phải là số nguyên dương" >&2
  exit 2
fi

PIP_CACHE_DIR="${DUB_NATIVE_ROOT}/cache/pip"
install -d -m 0755 -o root -g root "${DUB_NATIVE_ROOT}/cache"
install -d -m 0700 -o root -g root "${PIP_CACHE_DIR}"
export PIP_CACHE_DIR

"${PYTHON_BIN}" - <<'PY'
import sys
if not ((3, 11, 0, "candidate", 1) <= sys.version_info < (3, 13)):
    raise SystemExit(f"Cần Python 3.11 hoặc 3.12, hiện có {sys.version.split()[0]}")
PY

export DEBIAN_FRONTEND=noninteractive
apt-get update
QBIT_VERSION="$(jq -r '.components.qbittorrent_nox.version' "${PROJECT_ROOT}/native/components.lock.json")"
apt-get install -y \
  build-essential \
  ca-certificates \
  cmake \
  curl \
  ffmpeg \
  git \
  iproute2 \
  jq \
  ninja-build \
  "qbittorrent-nox=${QBIT_VERSION}" \
  sqlite3

bash "${SCRIPT_DIR}/install-llama-cpp.sh"

TIGER_COMMIT="$(jq -r '.components.tiger.commit' "${PROJECT_ROOT}/native/components.lock.json")"
TIGER_REPOSITORY="$(jq -r '.components.tiger.repository' "${PROJECT_ROOT}/native/components.lock.json")"
TIGER_OVERLAY_SHA256="$(jq -r '.components.tiger.compatibility_overlay_sha256' "${PROJECT_ROOT}/native/components.lock.json")"
TIGER_TARGET="/opt/tiger-${TIGER_COMMIT}"
if [[ ! -d "${TIGER_TARGET}/.git" ]]; then
  git init -q "${TIGER_TARGET}"
  git -C "${TIGER_TARGET}" remote add origin "${TIGER_REPOSITORY}"
  git -C "${TIGER_TARGET}" fetch --depth 1 origin "${TIGER_COMMIT}"
  git -C "${TIGER_TARGET}" checkout -q --detach FETCH_HEAD
fi
if [[ "$(git -C "${TIGER_TARGET}" rev-parse HEAD)" != "${TIGER_COMMIT}" ]]; then
  echo "Source TIGER hiện có không khớp commit đã khóa" >&2
  exit 2
fi
if ! printf '%s  %s\n' "${TIGER_OVERLAY_SHA256}" "${TIGER_TARGET}/look2hear/layers/__init__.py" | sha256sum --check --status; then
  if [[ -e "${TIGER_TARGET}/look2hear/layers/__init__.py" ]]; then
    chmod u+w "${TIGER_TARGET}/look2hear/layers/__init__.py"
  fi
  install -m 0444 "${PROJECT_ROOT}/native/tiger-layers-init.py" "${TIGER_TARGET}/look2hear/layers/__init__.py"
fi
printf '%s  %s\n' "${TIGER_OVERLAY_SHA256}" "${TIGER_TARGET}/look2hear/layers/__init__.py" | sha256sum --check --status
chmod -R a-w "${TIGER_TARGET}"
if [[ -e /opt/tiger && ! -L /opt/tiger ]]; then
  echo "/opt/tiger đã tồn tại nhưng không phải symlink do bootstrap quản lý" >&2
  exit 2
fi
ln -sfn "${TIGER_TARGET}" /opt/tiger

VIENEU_COMMIT="$(jq -r '.components.vieneu.commit' "${PROJECT_ROOT}/native/components.lock.json")"
VIENEU_REPOSITORY="$(jq -r '.components.vieneu.repository' "${PROJECT_ROOT}/native/components.lock.json")"
VIENEU_ENTRYPOINT_SHA256="$(jq -r '.components.vieneu.entrypoint_sha256' "${PROJECT_ROOT}/native/components.lock.json")"
VIENEU_ROOT="/opt/vieneu"
VIENEU_SOURCE_TARGET="${VIENEU_ROOT}/source-${VIENEU_COMMIT}"
install -d -m 0755 -o root -g root "${VIENEU_ROOT}"
if [[ ! -d "${VIENEU_SOURCE_TARGET}/.git" ]]; then
  git init -q "${VIENEU_SOURCE_TARGET}"
  git -C "${VIENEU_SOURCE_TARGET}" remote add origin "${VIENEU_REPOSITORY}"
  git -C "${VIENEU_SOURCE_TARGET}" fetch --depth 1 origin "${VIENEU_COMMIT}"
  git -C "${VIENEU_SOURCE_TARGET}" checkout -q --detach FETCH_HEAD
fi
if [[ "$(git -C "${VIENEU_SOURCE_TARGET}" rev-parse HEAD)" != "${VIENEU_COMMIT}" ]]; then
  echo "Source VieNeu hiện có không khớp commit đã khóa" >&2
  exit 2
fi
chmod -R a-w "${VIENEU_SOURCE_TARGET}"
if [[ -e "${VIENEU_ROOT}/source" && ! -L "${VIENEU_ROOT}/source" ]]; then
  echo "/opt/vieneu/source đã tồn tại nhưng không phải symlink do bootstrap quản lý" >&2
  exit 2
fi
ln -sfn "${VIENEU_SOURCE_TARGET}" "${VIENEU_ROOT}/source"
if ! printf '%s  %s\n' "${VIENEU_ENTRYPOINT_SHA256}" "${VIENEU_ROOT}/vieneu-offline.py" | sha256sum --check --status; then
  if [[ -e "${VIENEU_ROOT}/vieneu-offline.py" ]]; then
    chmod u+w "${VIENEU_ROOT}/vieneu-offline.py"
  fi
  install -m 0555 "${PROJECT_ROOT}/scripts/vieneu-offline.py" "${VIENEU_ROOT}/vieneu-offline.py"
fi
printf '%s  %s\n' "${VIENEU_ENTRYPOINT_SHA256}" "${VIENEU_ROOT}/vieneu-offline.py" | sha256sum --check --status

if ! id "${DUB_NATIVE_USER}" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "${DUB_NATIVE_ROOT}/home" --shell /usr/sbin/nologin "${DUB_NATIVE_USER}"
fi
for device_group in video render; do
  if getent group "${device_group}" >/dev/null 2>&1; then
    usermod -a -G "${device_group}" "${DUB_NATIVE_USER}"
  fi
done

install -d -m 0750 -o "${DUB_NATIVE_USER}" -g "${DUB_NATIVE_USER}" \
  "${DUB_NATIVE_ROOT}/state" \
  "${DUB_MODELS_DIR}" \
  "${DUB_INCOMING_DIR}" \
  "${DUB_JOBS_DIR}" \
  "${DUB_OUTPUT_DIR}" \
  "${DUB_NATIVE_ROOT}/secrets" \
  "${DUB_PROWLARR_DATA_DIR}" \
  "${DUB_QBITTORRENT_PROFILE}" \
  "${DUB_RUNTIME_RUN_DIR}" \
  "${DUB_RUNTIME_LOG_DIR}"
install -d -m 0755 -o root -g root "${DUB_NATIVE_ROOT}/opt"

QBIT_CONFIG_DIR="${DUB_QBITTORRENT_PROFILE}/qBittorrent/config"
QBIT_CONFIG_PATH="${QBIT_CONFIG_DIR}/qBittorrent.conf"
install -d -m 0750 -o "${DUB_NATIVE_USER}" -g "${DUB_NATIVE_USER}" "${QBIT_CONFIG_DIR}"
if [[ ! -e "${QBIT_CONFIG_PATH}" ]]; then
  install -m 0640 -o "${DUB_NATIVE_USER}" -g "${DUB_NATIVE_USER}" /dev/null "${QBIT_CONFIG_PATH}"
  printf '[Preferences]\nDownloads\\SavePath=%s/\nWebUI\\Address=127.0.0.1\nWebUI\\Port=8081\n' \
    "${DUB_INCOMING_DIR}" >"${QBIT_CONFIG_PATH}"
fi

PROWLARR_VERSION="$(jq -r '.components.prowlarr.version' "${PROJECT_ROOT}/native/components.lock.json")"
PROWLARR_URL="$(jq -r '.components.prowlarr.url' "${PROJECT_ROOT}/native/components.lock.json")"
PROWLARR_SHA256="$(jq -r '.components.prowlarr.sha256' "${PROJECT_ROOT}/native/components.lock.json")"
PROWLARR_TARGET="${DUB_NATIVE_ROOT}/opt/prowlarr-${PROWLARR_VERSION}"
if [[ ! -x "${PROWLARR_TARGET}/Prowlarr" ]]; then
  TEMP_DIR="$(mktemp -d)"
  trap 'rm -rf -- "${TEMP_DIR}"' EXIT
  curl --fail --location --retry 3 --output "${TEMP_DIR}/prowlarr.tar.gz" "${PROWLARR_URL}"
  printf '%s  %s\n' "${PROWLARR_SHA256}" "${TEMP_DIR}/prowlarr.tar.gz" | sha256sum --check --status
  tar -xzf "${TEMP_DIR}/prowlarr.tar.gz" -C "${TEMP_DIR}"
  install -d -m 0755 -o root -g root "${PROWLARR_TARGET}"
  cp -a "${TEMP_DIR}/Prowlarr/." "${PROWLARR_TARGET}/"
  chown -R root:root "${PROWLARR_TARGET}"
  chmod -R a-w "${PROWLARR_TARGET}"
fi
ln -sfn "${PROWLARR_TARGET}" "${DUB_NATIVE_ROOT}/opt/prowlarr"

if [[ ! -x "${DUB_VENV_DIR}/bin/python" ]]; then
  "${PYTHON_BIN}" -m venv --system-site-packages "${DUB_VENV_DIR}"
fi
"${DUB_VENV_DIR}/bin/python" -m pip install --disable-pip-version-check \
  --require-hashes --requirement "${PROJECT_ROOT}/requirements/native.lock"
"${DUB_VENV_DIR}/bin/python" -m pip install --disable-pip-version-check \
  --no-deps -e "${PROJECT_ROOT}"
if [[ "${INSTALL_TEST_MODE}" == full ]]; then
  "${DUB_VENV_DIR}/bin/python" -m pip install --disable-pip-version-check \
    "coverage==7.13.2" "pytest==9.1.1" "pytest-asyncio==1.4.0"
fi

"${DUB_VENV_DIR}/bin/python" -m pip check
"${DUB_VENV_DIR}/bin/python" -m compileall -q "${PROJECT_ROOT}/src"
"${DUB_VENV_DIR}/bin/python" - <<'PY'
import ctranslate2
import faster_whisper
import onnxruntime
import transformers

import dub_server.api
import dub_server.cli
import dub_server.worker
PY
if [[ "${INSTALL_TEST_MODE}" == full ]]; then
  "${DUB_VENV_DIR}/bin/python" -m pytest "${PROJECT_ROOT}/tests"
fi

chown -R "${DUB_NATIVE_USER}:${DUB_NATIVE_USER}" \
  "${DUB_NATIVE_ROOT}/state" \
  "${DUB_MODELS_DIR}" \
  "${DUB_NATIVE_ROOT}/data" \
  "${DUB_NATIVE_ROOT}/secrets" \
  "${DUB_PROWLARR_DATA_DIR}" \
  "${DUB_QBITTORRENT_PROFILE}" \
  "${DUB_RUNTIME_RUN_DIR}" \
  "${DUB_RUNTIME_LOG_DIR}"

runuser -u "${DUB_NATIVE_USER}" --preserve-environment -- \
  "${DUB_VENV_DIR}/bin/python" -m dub_server.worker --once

echo "Bootstrap native hoàn tất tại ${DUB_NATIVE_ROOT}"

#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(CDPATH= cd -- "${SCRIPT_DIR}/.." && pwd)"
LOCK_PATH="${PROJECT_ROOT}/native/components.lock.json"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Trinh cai llama.cpp phai chay bang root" >&2
  exit 2
fi

LLAMA_RELEASE="$(jq -er '.components.llama_cpp.release' "${LOCK_PATH}")"
LLAMA_COMMIT="$(jq -er '.components.llama_cpp.commit' "${LOCK_PATH}")"
LLAMA_REPOSITORY="$(jq -er '.components.llama_cpp.repository' "${LOCK_PATH}")"
LOCKED_LLAMA_DEFAULT_CUDA_VERSION="$(
  jq -er '.components.llama_cpp.cuda_version' "${LOCK_PATH}"
)"
LOCKED_LLAMA_CUDA_VERSIONS="$(
  jq -er '
    .components.llama_cpp.cuda_supported_versions as $versions
    | if ($versions | type) == "array" and ($versions | length) > 0
        and ($versions | all(.[]; type == "string" and test("^[0-9]+\\.[0-9]+$")))
        and $versions == ($versions | sort | unique)
      then $versions | join(";")
      else error("invalid cuda_supported_versions")
      end
  ' "${LOCK_PATH}"
)"
LOCKED_LLAMA_CUDA_ARCHITECTURES="$(
  jq -er '
    .components.llama_cpp.cuda_supported_architectures
    | if type == "array" and length > 0
        and all(.[]; type == "number" and floor == .)
      then map(tostring) | join(";")
      else error("invalid cuda_supported_architectures")
      end
  ' "${LOCK_PATH}"
)"
LOCKED_LLAMA_DEFAULT_CUDA_ARCHITECTURE="$(
  jq -er '.components.llama_cpp.cuda_default_build_architecture | tostring' \
    "${LOCK_PATH}"
)"
LLAMA_CUDA_ARCHITECTURES="${DUB_LLAMA_CUDA_ARCHITECTURES:-${LOCKED_LLAMA_DEFAULT_CUDA_ARCHITECTURE}}"
LLAMA_CUDA_ARCH_LABEL="${LLAMA_CUDA_ARCHITECTURES//;/_}"
LLAMA_LINK="/usr/local/lib/llama.cpp"
NVCC_PATH="${CUDACXX:-/usr/local/cuda/bin/nvcc}"

if [[ ! "${LLAMA_COMMIT}" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Commit llama.cpp trong components.lock.json khong hop le" >&2
  exit 2
fi
if [[ ! -x "${NVCC_PATH}" ]]; then
  echo "Khong tim thay nvcc tai ${NVCC_PATH}" >&2
  exit 2
fi
ACTUAL_NVCC_RELEASE="$(
  "${NVCC_PATH}" --version \
    | sed -n 's/.*release \([0-9][0-9]*\.[0-9][0-9]*\).*/\1/p' \
    | tail -n 1
)"
if ! tr ';' '\n' <<<"${LOCKED_LLAMA_CUDA_VERSIONS}" \
    | grep -Fxq -- "${ACTUAL_NVCC_RELEASE}"; then
  echo "CUDA toolkit ${ACTUAL_NVCC_RELEASE:-khong xac dinh} khong nam trong ma tran ho tro ${LOCKED_LLAMA_CUDA_VERSIONS//;/, }" >&2
  exit 2
fi
if ! tr ';' '\n' <<<"${LOCKED_LLAMA_CUDA_VERSIONS}" \
    | grep -Fxq -- "${LOCKED_LLAMA_DEFAULT_CUDA_VERSION}"; then
  echo "CUDA toolkit mac dinh khong nam trong ma tran ho tro" >&2
  exit 2
fi
LLAMA_CUDA_VERSION="${ACTUAL_NVCC_RELEASE}"
LLAMA_TARGET="/usr/local/lib/llama.cpp-${LLAMA_RELEASE}-cuda${LLAMA_CUDA_VERSION}-sm${LLAMA_CUDA_ARCH_LABEL}-offline"
if [[ ! "${LLAMA_CUDA_ARCHITECTURES}" =~ ^[0-9]{2,3}(\;[0-9]{2,3})*$ ]]; then
  echo "Danh sach CUDA architecture khong hop le: ${LLAMA_CUDA_ARCHITECTURES}" >&2
  exit 2
fi
nvcc_architectures="$(${NVCC_PATH} --list-gpu-arch 2>/dev/null)" || {
  echo "Khong doc duoc danh sach architecture tu nvcc" >&2
  exit 2
}
IFS=';' read -r -a requested_architectures <<<"${LLAMA_CUDA_ARCHITECTURES}"
for architecture in "${requested_architectures[@]}"; do
  if ! tr ';' '\n' <<<"${LOCKED_LLAMA_CUDA_ARCHITECTURES}" \
      | grep -Fxq -- "${architecture}"; then
    echo "CUDA architecture sm_${architecture} khong nam trong ma tran ho tro" >&2
    exit 2
  fi
  if ! printf '%s\n' "${nvcc_architectures}" | grep -Fxq -- "compute_${architecture}"; then
    echo "nvcc khong the build cho sm_${architecture}" >&2
    exit 2
  fi
done

if [[ -e "${LLAMA_TARGET}" || -L "${LLAMA_TARGET}" ]]; then
  if [[ -d "${LLAMA_TARGET}" && ! -L "${LLAMA_TARGET}" \
      && -x "${LLAMA_TARGET}/llama-server" \
      && -x "${LLAMA_TARGET}/llama-cli" ]] \
      && "${LLAMA_TARGET}/llama-server" --version 2>&1 | grep -Fq "${LLAMA_COMMIT:0:7}" \
      && jq -e \
        --arg release "${LLAMA_RELEASE}" \
        --arg commit "${LLAMA_COMMIT}" \
        --arg cuda_version "${LLAMA_CUDA_VERSION}" \
        --arg cuda_architectures "${LLAMA_CUDA_ARCHITECTURES}" \
        --arg llama_server_sha256 "$(sha256sum "${LLAMA_TARGET}/llama-server" | awk '{print $1}')" \
        --arg llama_cli_sha256 "$(sha256sum "${LLAMA_TARGET}/llama-cli" | awk '{print $1}')" \
        '.release == $release and .commit == $commit and
          .cuda_version == $cuda_version and
          .cuda_architectures == $cuda_architectures and
          .binaries.llama_server_sha256 == $llama_server_sha256 and
          .binaries.llama_cli_sha256 == $llama_cli_sha256' \
        "${LLAMA_TARGET}/build-receipt.json" >/dev/null 2>&1; then
    ln -sfnT "${LLAMA_TARGET}" "${LLAMA_LINK}"
    exit 0
  fi
  echo "Thu muc llama.cpp dich da ton tai nhung receipt khong khop: ${LLAMA_TARGET}" >&2
  exit 2
fi

TEMP_DIR="$(mktemp -d)"
trap 'rm -rf -- "${TEMP_DIR}"' EXIT
SOURCE_DIR="${TEMP_DIR}/source"
BUILD_DIR="${TEMP_DIR}/build"
STAGE_DIR="${TEMP_DIR}/runtime"

git init -q "${SOURCE_DIR}"
git -C "${SOURCE_DIR}" remote add origin "${LLAMA_REPOSITORY}"
git -C "${SOURCE_DIR}" fetch --depth 1 origin "${LLAMA_COMMIT}"
git -C "${SOURCE_DIR}" checkout -q --detach FETCH_HEAD
if [[ "$(git -C "${SOURCE_DIR}" rev-parse HEAD)" != "${LLAMA_COMMIT}" ]]; then
  echo "Source llama.cpp khong dung commit da khoa" >&2
  exit 2
fi

cmake -S "${SOURCE_DIR}" -B "${BUILD_DIR}" -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_BUILD_RPATH_USE_ORIGIN=ON \
  -DCMAKE_CUDA_COMPILER="${NVCC_PATH}" \
  -DCMAKE_CUDA_ARCHITECTURES="${LLAMA_CUDA_ARCHITECTURES}" \
  -DGGML_CUDA=ON \
  -DLLAMA_CURL=OFF \
  -DLLAMA_BUILD_UI=OFF \
  -DLLAMA_USE_PREBUILT_UI=OFF \
  -DLLAMA_BUILD_TESTS=OFF \
  -DLLAMA_BUILD_EXAMPLES=ON
cmake --build "${BUILD_DIR}" --target llama-server llama-cli --parallel "${DUB_BUILD_JOBS:-4}"

install -d -m 0755 "${STAGE_DIR}"
cp -a "${BUILD_DIR}/bin/." "${STAGE_DIR}/"
if ! LD_LIBRARY_PATH="${STAGE_DIR}" "${STAGE_DIR}/llama-server" --version 2>&1 \
    | grep -Fq "${LLAMA_COMMIT:0:7}"; then
  echo "Binary llama-server vua build khong dung commit da khoa" >&2
  exit 2
fi
jq -n \
  --arg release "${LLAMA_RELEASE}" \
  --arg commit "${LLAMA_COMMIT}" \
  --arg cuda_version "${LLAMA_CUDA_VERSION}" \
  --arg cuda_architectures "${LLAMA_CUDA_ARCHITECTURES}" \
  --arg llama_server_sha256 "$(sha256sum "${STAGE_DIR}/llama-server" | awk '{print $1}')" \
  --arg llama_cli_sha256 "$(sha256sum "${STAGE_DIR}/llama-cli" | awk '{print $1}')" \
  '{schema_version:1, release:$release, commit:$commit,
    cuda_version:$cuda_version, cuda_architectures:$cuda_architectures,
    binaries:{llama_server_sha256:$llama_server_sha256,
      llama_cli_sha256:$llama_cli_sha256}}' \
  >"${STAGE_DIR}/build-receipt.json"
chown -R root:root "${STAGE_DIR}"
chmod -R a-w "${STAGE_DIR}"
mv -T -- "${STAGE_DIR}" "${LLAMA_TARGET}"
ln -sfnT "${LLAMA_TARGET}" "${LLAMA_LINK}"

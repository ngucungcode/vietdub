#!/usr/bin/env bash

# This file is sourced by install.sh after the requested Git ref has been
# fetched into a private staging directory. It intentionally does not source
# configuration from the legacy deployment before validating the source tree.

MIGRATION_BACKUP_PATH=""
MIGRATION_FAILED_SOURCE_PATH=""
MIGRATION_EFFECTIVE_DATA_DIR=""
MIGRATION_OLD_STACK_MODE="none"
MIGRATION_MOVED_ITEMS=()
MIGRATED_RUNTIME_REUSABLE=false
MIGRATION_SIGNAL_ARMED=false
MIGRATION_SIGNAL_TARGET=""
MIGRATION_SIGNAL_BACKUP=""
MIGRATION_SIGNAL_FAILED=""
MIGRATION_SIGNAL_STACK_MODE="none"
MIGRATION_SIGNAL_EXPECTED_ITEMS=()
MIGRATION_JOURNAL_PATH=""
MIGRATION_DATA_ROOT_DEVICE=""
MIGRATION_DATA_ROOT_INODE=""
MIGRATION_DATA_ROOT_UID=""
MIGRATION_DATA_ROOT_GID=""
MIGRATION_DATA_ROOT_MODE=""

migration_log() {
  printf '[thuyet-minh] %s\n' "$*"
}

migration_error() {
  printf '[thuyet-minh] Lỗi migration: %s\n' "$*" >&2
}

migration_path_exists() {
  [[ -e "$1" || -L "$1" ]]
}

migration_write_journal() {
  local phase="$1"
  shift
  python3 - \
    "${MIGRATION_JOURNAL_PATH}" "${phase}" \
    "${MIGRATION_SIGNAL_TARGET}" "${MIGRATION_SIGNAL_BACKUP}" \
    "${MIGRATION_SIGNAL_FAILED}" "${MIGRATION_EFFECTIVE_DATA_DIR}" \
    "${MIGRATION_SIGNAL_STACK_MODE}" \
    "${MIGRATION_DATA_ROOT_DEVICE}" "${MIGRATION_DATA_ROOT_INODE}" \
    "${MIGRATION_DATA_ROOT_UID}" "${MIGRATION_DATA_ROOT_GID}" \
    "${MIGRATION_DATA_ROOT_MODE}" "$@" <<'PY'
import json
import os
from pathlib import Path
import sys

path = Path(sys.argv[1])
document = {
    "schema_version": 2,
    "phase": sys.argv[2],
    "target": sys.argv[3],
    "backup": sys.argv[4],
    "failed_new": sys.argv[5],
    "data_root": sys.argv[6],
    "stack_mode": sys.argv[7],
    "data_root_identity": None if not sys.argv[8] else {
        "device": sys.argv[8],
        "inode": sys.argv[9],
        "uid": sys.argv[10],
        "gid": sys.argv[11],
        "mode": sys.argv[12],
    },
    "moved_items": sys.argv[13:],
}
temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
payload = (json.dumps(document, ensure_ascii=False, sort_keys=True) + "\n").encode()
descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
try:
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
finally:
    if temporary.exists():
        temporary.unlink()
PY
}

migration_clear_journal() {
  [[ -n "${MIGRATION_JOURNAL_PATH}" ]] || return 0
  rm -f -- "${MIGRATION_JOURNAL_PATH}" || return 1
  python3 - "$(dirname -- "${MIGRATION_JOURNAL_PATH}")" <<'PY'
import os
from pathlib import Path
import sys

directory = os.open(Path(sys.argv[1]), os.O_RDONLY)
try:
    os.fsync(directory)
finally:
    os.close(directory)
PY
}

migration_validate_project_tree() {
  local project_root="$1"
  local tree_role="${2:-legacy}"
  python3 - "${project_root}" "${tree_role}" <<'PY'
import json
import os
from pathlib import Path
import sys
import tomllib

root = Path(sys.argv[1])
tree_role = sys.argv[2]
if not root.is_absolute() or root.is_symlink() or not root.is_dir():
    raise SystemExit("project root không phải thư mục tuyệt đối, thực")

required_files = (
    "pyproject.toml",
    ".env.native.example",
    "LICENSE",
    "config/models.lock.json",
    "native/components.lock.json",
    "native/supervisord.conf",
    "src/dub_server/__init__.py",
    "src/dub_server/cli.py",
    "scripts/native-common.sh",
    "scripts/native-bootstrap.sh",
    "scripts/install-llama-cpp.sh",
    "scripts/native-stack.sh",
    "scripts/vieneu-offline.py",
)
if tree_role == "staged":
    required_files += (
        "installer/prepare-data-root.sh",
        "scripts/dub-wrapper.sh",
        "scripts/generate-sbom.py",
        "scripts/native-acceptance.sh",
        "scripts/native-init-services.sh",
        "scripts/native-model.sh",
        "scripts/native-phase2-acceptance.py",
        "scripts/native-phase2-acceptance.sh",
        "scripts/native-phase3-acceptance.py",
        "scripts/native-phase3-acceptance.sh",
        "scripts/native-phase4-acceptance.sh",
        "scripts/native-preflight.sh",
        "scripts/native-qbittorrent-smoke.py",
        "scripts/phase4_acceptance.py",
    )
for relative in required_files:
    path = root / relative
    if path.is_symlink() or not path.is_file():
        raise SystemExit(f"thiếu marker source hợp lệ: {relative}")

with (root / "pyproject.toml").open("rb") as handle:
    project = tomllib.load(handle).get("project", {})
if project.get("name") != "thuyet-minh-offline-gpu":
    raise SystemExit("project.name không khớp")
if project.get("scripts", {}).get("dub") != "dub_server.cli:app":
    raise SystemExit("entrypoint dub không khớp")

with (root / "config/models.lock.json").open(encoding="utf-8") as handle:
    models_document = json.load(handle)
model_ids = {
    item.get("id") for item in models_document.get("models", [])
    if isinstance(item, dict)
}

required_models = {
    "asr-faster-whisper-small",
    "mt-gemma4-e2b-q4",
    "separation-tiger-dnr",
    "tts-piper-vi-vais1000-medium",
}
if models_document.get("schema_version") != 1 or not required_models <= model_ids:
    raise SystemExit("models.lock.json không phải catalog của dự án")

with (root / "native/components.lock.json").open(encoding="utf-8") as handle:
    components_document = json.load(handle)
components = components_document.get("components", {})
if components_document.get("schema_version") != 1 or not {
    "llama_cpp", "prowlarr", "qbittorrent_nox"
} <= set(components):
    raise SystemExit("components.lock.json không phải catalog của dự án")

executable_files = ["scripts/native-bootstrap.sh", "scripts/native-stack.sh"]
if tree_role == "staged":
    executable_files += [
        "installer/prepare-data-root.sh",
        "scripts/dub-wrapper.sh",
        "scripts/native-acceptance.sh",
        "scripts/native-init-services.sh",
        "scripts/native-model.sh",
        "scripts/native-phase2-acceptance.sh",
        "scripts/native-phase3-acceptance.sh",
        "scripts/native-phase4-acceptance.sh",
        "scripts/native-preflight.sh",
    ]
for relative in executable_files:
    if not os.access(root / relative, os.X_OK):
        raise SystemExit(f"script không executable: {relative}")
PY
}

migration_read_configured_data_root() {
  local env_path="$1"
  python3 - "${env_path}" <<'PY'
from pathlib import Path
import os
import re
import sys

path = Path(sys.argv[1])
if not path.exists():
    raise SystemExit(0)

matches = []
for raw_line in path.read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#"):
        continue
    if re.match(r"(?:export[ \t]+)?DUB_NATIVE_ROOT\b", line) \
            and not line.startswith("DUB_NATIVE_ROOT="):
        raise SystemExit("Chỉ chấp nhận DUB_NATIVE_ROOT=/absolute/path dạng literal")
    if line.startswith("DUB_NATIVE_ROOT="):
        matches.append(line.removeprefix("DUB_NATIVE_ROOT="))

if len(matches) > 1:
    raise SystemExit(".env.native khai báo DUB_NATIVE_ROOT nhiều lần")
if not matches:
    raise SystemExit(0)

value = matches[0]
if not re.fullmatch(r"/[A-Za-z0-9._/+:-]+", value):
    raise SystemExit("DUB_NATIVE_ROOT phải là đường dẫn tuyệt đối literal")
if os.path.normpath(value) != value:
    raise SystemExit("DUB_NATIVE_ROOT chứa thành phần đường dẫn không chuẩn")
print(value)
PY
}

migration_runtime_fingerprint() {
  local project_root="$1"
  (
    cd -- "${project_root}" || exit 1
    {
      sha256sum \
        pyproject.toml \
        .env.native.example \
        config/models.lock.json \
        scripts/native-bootstrap.sh \
        scripts/native-common.sh \
        scripts/install-llama-cpp.sh \
        scripts/native-stack.sh \
        scripts/vieneu-offline.py
      find native -maxdepth 1 -type f -print0 \
        | sort -z \
        | xargs -0 sha256sum
    } | sha256sum | awk '{print $1}'
  )
}

migration_runtime_compatibility_fingerprint() {
  local project_root="$1"
  local native_components_manifest
  local project_manifest
  (
    set -o pipefail
    cd -- "${project_root}" || exit 1
    project_manifest="$(python3 - pyproject.toml <<'PY'
import json
from pathlib import Path
import sys
import tomllib

path = Path(sys.argv[1])
with path.open("rb") as handle:
    document = tomllib.load(handle)
project = document.get("project")
if not isinstance(project, dict):
    raise SystemExit("pyproject.toml thiếu bảng project")
# Release metadata is intentionally excluded. The environment example, native
# bootstrap procedure, architecture-aware llama.cpp installer and native stack
# control script are also excluded because none of them changes an
# already-installed runtime during a compatible atomic upgrade. The new source
# supplies those cold-bootstrap procedures after the switch. Persistent
# dependencies, build configuration, entrypoints, model locks and native runtime
# files remain gated. A bootstrap change that alters a persistent artifact must
# also update its owning lock or dependency manifest.
project.pop("version", None)
print(json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
PY
    )" || exit 1
    native_components_manifest="$(python3 - native/components.lock.json <<'PY'
import json
from pathlib import Path
import sys

with Path(sys.argv[1]).open(encoding="utf-8") as handle:
    document = json.load(handle)
components = document.get("components")
if not isinstance(components, dict):
    raise SystemExit("native components lock không hợp lệ")
llama = components.get("llama_cpp")
if not isinstance(llama, dict):
    raise SystemExit("native components lock thiếu llama_cpp")

# The installed CUDA architecture is gated separately against install-state.
# Releases through v0.3.2 represented the sm_86 bootstrap default with the
# ambiguous `cuda_architectures` field; newer releases split the supported
# matrix from the default/actual build. These source-only keys do not change an
# existing native runtime, while every other component field remains hashed.
for key in (
    "cuda_architectures",
    "cuda_supported_versions",
    "cuda_supported_architectures",
    "cuda_default_build_architecture",
):
    llama.pop(key, None)
print(json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
PY
    )" || exit 1
    {
      printf '%s\n' "${project_manifest}" || exit 1
      printf '%s\n' "${native_components_manifest}" || exit 1
      sha256sum \
        config/models.lock.json \
        scripts/native-common.sh \
        scripts/vieneu-offline.py || exit 1
      find native -maxdepth 1 -type f ! -name components.lock.json -print0 \
        | sort -z \
        | xargs -0 sha256sum || exit 1
    } | sha256sum | awk '{print $1}'
  )
}

migration_installed_cuda_architecture() {
  local project_root="$1"
  local data_root="$2"
  local state_path="${data_root}/install-state.json"

  if migration_path_exists "${state_path}"; then
    [[ -f "${state_path}" && ! -L "${state_path}" ]] || return 1
    python3 - "${state_path}" <<'PY'
import json
from pathlib import Path
import re
import sys

with Path(sys.argv[1]).open(encoding="utf-8") as handle:
    document = json.load(handle)
gpu = document.get("gpu")
architecture = gpu.get("cuda_architecture") if isinstance(gpu, dict) else None
if architecture is None:
    # Releases through v0.3.2 only produced a native sm_86 llama.cpp runtime.
    architecture = "sm_86"
if not isinstance(architecture, str) or not re.fullmatch(r"sm_[0-9]{2,3}", architecture):
    raise SystemExit("install-state CUDA architecture không hợp lệ")
print(architecture)
PY
    return
  fi

  python3 - "${project_root}/native/components.lock.json" <<'PY'
import json
from pathlib import Path
import re
import sys

with Path(sys.argv[1]).open(encoding="utf-8") as handle:
    document = json.load(handle)
architecture = document.get("components", {}).get("llama_cpp", {}).get("cuda_architectures")
if not isinstance(architecture, str) or not re.fullmatch(r"[0-9]{2,3}", architecture):
    raise SystemExit("không suy ra được CUDA architecture của runtime legacy")
print(f"sm_{architecture}")
PY
}

migration_installed_cuda_toolkit_version() {
  local project_root="$1"
  local data_root="$2"
  local state_path="${data_root}/install-state.json"
  local lock_path="${project_root}/native/components.lock.json"

  if migration_path_exists "${state_path}"; then
    [[ -f "${state_path}" && ! -L "${state_path}" ]] || return 1
  fi
  [[ -f "${lock_path}" && ! -L "${lock_path}" ]] || return 1
  python3 - "${state_path}" "${lock_path}" <<'PY'
import json
from pathlib import Path
import re
import sys

state_path = Path(sys.argv[1])
version = None
if state_path.exists():
    with state_path.open(encoding="utf-8") as handle:
        state = json.load(handle)
    gpu = state.get("gpu")
    version = gpu.get("cuda_toolkit_version") if isinstance(gpu, dict) else None
    installer_version = state.get("installer_version")
    if version is None:
        if not isinstance(installer_version, str) or not re.fullmatch(
            r"[0-9]+\.[0-9]+\.[0-9]+", installer_version
        ):
            raise SystemExit("install-state không ghi CUDA toolkit và version hợp lệ")
        version_parts = tuple(int(part) for part in installer_version.split("."))
        if version_parts >= (0, 3, 4):
            raise SystemExit("install-state hiện tại thiếu CUDA toolkit")
if version is None:
    with Path(sys.argv[2]).open(encoding="utf-8") as handle:
        lock = json.load(handle)
    version = lock.get("components", {}).get("llama_cpp", {}).get("cuda_version")
if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+", version):
    raise SystemExit("không suy ra được CUDA toolkit của runtime đang cài")
print(version)
PY
}

migration_project_version() {
  local project_root="$1"
  python3 - "${project_root}/pyproject.toml" <<'PY'
from pathlib import Path
import re
import sys
import tomllib

with Path(sys.argv[1]).open("rb") as handle:
    version = tomllib.load(handle).get("project", {}).get("version")
if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
    raise SystemExit("project.version không phải SemVer phát hành")
print(version)
PY
}

migration_version_is_compatible() {
  local current_version="$1"
  local compatible_versions="$2"
  local -a candidates=()
  local candidate

  read -r -a candidates <<<"${compatible_versions}"
  for candidate in "${candidates[@]}"; do
    if [[ "${candidate}" == "${current_version}" ]]; then
      return 0
    fi
  done
  return 1
}

migration_validate_git_tree() {
  local project_root="$1"
  local resolved_root
  local worktree_root

  git -C "${project_root}" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
    || return 1
  resolved_root="$(readlink -f -- "${project_root}")" || return 1
  worktree_root="$(git -C "${project_root}" rev-parse --show-toplevel)" \
    || return 1
  worktree_root="$(readlink -f -- "${worktree_root}")" || return 1
  [[ "${resolved_root}" == "${worktree_root}" ]] || return 1
  git -C "${project_root}" diff --quiet -- \
    || return 1
  git -C "${project_root}" diff --cached --quiet -- \
    || return 1
  [[ -z "$(git -C "${project_root}" status --porcelain)" ]] \
    || return 1
  git -C "${project_root}" fsck --no-dangling >/dev/null \
    || return 1
}

migrate_git_release_upgrade() {
  local current_root="${1%/}"
  local staged_root="${2%/}"
  local requested_data_dir="${3:-${current_root}/var}"
  local data_dir_explicit="${4:-false}"
  local expected_target_version="$5"
  local compatible_versions="$6"
  local expected_cuda_architecture="${7:-}"
  local expected_cuda_toolkit_version="${8:-}"
  local current_version
  local target_version
  local current_commit
  local configured_data_dir=""
  local effective_data_dir
  local state_path
  local state_values
  local state_version
  local state_commit

  migration_validate_git_tree "${current_root}" || {
    migration_error "Git deployment hiện tại không sạch hoặc không hợp lệ"
    return 1
  }
  migration_validate_git_tree "${staged_root}" || {
    migration_error "Git source staging không sạch hoặc không hợp lệ"
    return 1
  }
  current_version="$(migration_project_version "${current_root}")" || {
    migration_error "Không đọc được phiên bản deployment hiện tại"
    return 1
  }
  target_version="$(migration_project_version "${staged_root}")" || {
    migration_error "Không đọc được phiên bản source staging"
    return 1
  }
  [[ "${target_version}" == "${expected_target_version}" ]] || {
    migration_error "Source staging có phiên bản ${target_version}, installer yêu cầu ${expected_target_version}"
    return 1
  }
  [[ "${current_version}" != "${target_version}" ]] || {
    migration_error "Deployment đã ở phiên bản ${target_version}; không có nâng cấp cần thực hiện"
    return 1
  }
  migration_version_is_compatible "${current_version}" "${compatible_versions}" || {
    migration_error "Không hỗ trợ nâng cấp ${current_version} -> ${target_version}"
    return 1
  }

  if migration_path_exists "${current_root}/.env.native"; then
    [[ -f "${current_root}/.env.native" \
      && ! -L "${current_root}/.env.native" ]] || {
      migration_error ".env.native phải là regular file, không phải symlink"
      return 1
    }
    configured_data_dir="$(
      migration_read_configured_data_root "${current_root}/.env.native"
    )" || {
      migration_error "Không thể đọc DUB_NATIVE_ROOT an toàn từ .env.native"
      return 1
    }
  fi
  if [[ -n "${configured_data_dir}" ]]; then
    if [[ "${data_dir_explicit}" == true \
      && "${requested_data_dir}" != "${configured_data_dir}" ]]; then
      migration_error "--data-dir không khớp DUB_NATIVE_ROOT hiện có"
      return 1
    fi
    effective_data_dir="${configured_data_dir}"
  else
    effective_data_dir="${requested_data_dir}"
  fi
  state_path="${effective_data_dir}/install-state.json"
  [[ -f "${state_path}" && ! -L "${state_path}" ]] || {
    migration_error "Thiếu install-state.json hợp lệ tại ${state_path}"
    return 1
  }
  state_values="$(python3 - "${state_path}" <<'PY'
import json
from pathlib import Path
import re
import sys

with Path(sys.argv[1]).open(encoding="utf-8") as handle:
    document = json.load(handle)
version = document.get("installer_version")
commit = document.get("commit")
if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
    raise SystemExit("install-state installer_version không hợp lệ")
if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40,64}", commit):
    raise SystemExit("install-state commit không hợp lệ")
print(version)
print(commit)
PY
  )" || {
    migration_error "Không đọc được install-state.json"
    return 1
  }
  state_version="$(printf '%s\n' "${state_values}" | sed -n '1p')"
  state_commit="$(printf '%s\n' "${state_values}" | sed -n '2p')"
  current_commit="$(git -C "${current_root}" rev-parse HEAD)" || return 1
  [[ "${state_version}" == "${current_version}" \
    && "${state_commit}" == "${current_commit}" ]] || {
    migration_error "install-state.json không khớp source Git hiện tại"
    return 1
  }

  migrate_legacy_install \
    "${current_root}" "${staged_root}" "${requested_data_dir}" \
    "${data_dir_explicit}" git "${expected_cuda_architecture}" \
    "${expected_cuda_toolkit_version}"
}

migration_restart_stack() {
  local project_root="$1"
  local stack_mode="$2"
  case "${stack_mode}" in
    systemd) systemctl start thuyet-minh-offline.service || return 1 ;;
    systemd-stopped) return 0 ;;
    native) "${project_root}/scripts/native-stack.sh" start 9>&- || return 1 ;;
    none) return 0 ;;
    *) return 1 ;;
  esac
  migration_wait_for_stack_health "${project_root}"
}

migration_stack_health_output() {
  local project_root="$1"
  local status_output

  status_output="$("${project_root}/scripts/native-stack.sh" status 2>/dev/null)" \
    || return 1
  migration_stack_status_text_healthy "${status_output}"
}

migration_stack_status_text_healthy() {
  local status_output="$1"
  local program

  for program in api prowlarr qbittorrent worker; do
    printf '%s\n' "${status_output}" \
      | grep -Eq "^${program}[[:space:]]+RUNNING[[:space:]]" || return 1
  done
  ! printf '%s\n' "${status_output}" \
    | grep -Eq ' (BACKOFF|EXITED|FATAL|STOPPED|UNKNOWN) '
}

migration_native_supervisor_present() {
  local project_root="$1"
  local data_root="$2"
  local pid_file="${data_root}/run/supervisord.pid"
  local supervisor_pid=""

  if [[ -r "${pid_file}" ]]; then
    supervisor_pid="$(<"${pid_file}")"
    if [[ "${supervisor_pid}" =~ ^[0-9]+$ ]] \
      && kill -0 "${supervisor_pid}" 2>/dev/null; then
      return 0
    fi
  fi
  if [[ -S "${data_root}/run/supervisor.sock" ]]; then
    return 0
  fi
  python3 - "${project_root}/native/supervisord.conf" <<'PY'
import os
from pathlib import Path
import sys

expected = os.path.realpath(sys.argv[1])
for entry in Path("/proc").iterdir():
    if not entry.name.isdigit():
        continue
    try:
        parts = (entry / "cmdline").read_bytes().split(b"\0")
        arguments = [part.decode(errors="replace") for part in parts if part]
    except (OSError, PermissionError):
        continue
    for index, argument in enumerate(arguments[:-1]):
        if argument != "-c" or os.path.realpath(arguments[index + 1]) != expected:
            continue
        if any("supervisord" in os.path.basename(item) for item in arguments[:index]):
            raise SystemExit(0)
raise SystemExit(1)
PY
}

migration_wait_for_stack_health() {
  local project_root="$1"
  local attempt
  for attempt in {1..30}; do
    if migration_stack_health_output "${project_root}"; then
      return 0
    fi
    sleep 1
  done
  return 1
}

migration_capture_data_root_identity() {
  local data_root="$1"
  if [[ ! -e "${data_root}" && ! -L "${data_root}" ]]; then
    return 0
  fi
  [[ -d "${data_root}" && ! -L "${data_root}" \
    && "$(readlink -f -- "${data_root}")" == "${data_root}" ]] || return 1
  MIGRATION_DATA_ROOT_DEVICE="$(stat -c '%d' -- "${data_root}")"
  MIGRATION_DATA_ROOT_INODE="$(stat -c '%i' -- "${data_root}")"
  MIGRATION_DATA_ROOT_UID="$(stat -c '%u' -- "${data_root}")"
  MIGRATION_DATA_ROOT_GID="$(stat -c '%g' -- "${data_root}")"
  MIGRATION_DATA_ROOT_MODE="$(stat -c '%a' -- "${data_root}")"
}

migration_verify_data_root_identity() {
  [[ -z "${MIGRATION_DATA_ROOT_DEVICE}" ]] && return 0
  [[ -d "${MIGRATION_EFFECTIVE_DATA_DIR}" \
    && ! -L "${MIGRATION_EFFECTIVE_DATA_DIR}" ]] || return 1
  [[ "$(stat -c '%d:%i:%u:%g:%a' -- "${MIGRATION_EFFECTIVE_DATA_DIR}")" \
    == "${MIGRATION_DATA_ROOT_DEVICE}:${MIGRATION_DATA_ROOT_INODE}:${MIGRATION_DATA_ROOT_UID}:${MIGRATION_DATA_ROOT_GID}:${MIGRATION_DATA_ROOT_MODE}" ]]
}

migration_restore_data_root_identity() {
  [[ -z "${MIGRATION_DATA_ROOT_DEVICE}" ]] && return 0
  [[ -d "${MIGRATION_EFFECTIVE_DATA_DIR}" \
    && ! -L "${MIGRATION_EFFECTIVE_DATA_DIR}" ]] || return 1
  [[ "$(stat -c '%d:%i' -- "${MIGRATION_EFFECTIVE_DATA_DIR}")" \
    == "${MIGRATION_DATA_ROOT_DEVICE}:${MIGRATION_DATA_ROOT_INODE}" ]] || return 1
  chown "${MIGRATION_DATA_ROOT_UID}:${MIGRATION_DATA_ROOT_GID}" \
    -- "${MIGRATION_EFFECTIVE_DATA_DIR}" || return 1
  chmod "${MIGRATION_DATA_ROOT_MODE}" -- "${MIGRATION_EFFECTIVE_DATA_DIR}" \
    || return 1
  migration_verify_data_root_identity
}

migration_recover_before_source_switch() {
  local project_root="$1"
  local stack_mode="$2"
  shift 2
  if migration_restart_stack "${project_root}" "${stack_mode}"; then
    migration_clear_journal
    return $?
  fi
  migration_write_journal rollback_incomplete "$@" || true
  migration_error "Source chưa đổi nhưng trạng thái stack chưa phục hồi; giữ journal"
  return 1
}

migration_abort_on_signal() {
  local signal_status="$1"
  local inferred_items=()
  local item
  local recovery_ok=true
  trap - INT TERM HUP

  if [[ "${MIGRATION_SIGNAL_ARMED}" == true ]]; then
    MIGRATION_SIGNAL_ARMED=false
    migration_error "Nhận tín hiệu dừng; đang phục hồi transaction migration"
    if migration_path_exists "${MIGRATION_SIGNAL_BACKUP}"; then
      if migration_path_exists "${MIGRATION_SIGNAL_TARGET}"; then
        for item in "${MIGRATION_SIGNAL_EXPECTED_ITEMS[@]}"; do
          if migration_path_exists "${MIGRATION_SIGNAL_TARGET}/${item}" \
            && ! migration_path_exists "${MIGRATION_SIGNAL_BACKUP}/${item}"; then
            inferred_items+=("${item}")
          fi
        done
        migration_rollback_switch \
          "${MIGRATION_SIGNAL_TARGET}" "${MIGRATION_SIGNAL_BACKUP}" \
          "${MIGRATION_SIGNAL_FAILED}" "${MIGRATION_SIGNAL_STACK_MODE}" \
          true "${inferred_items[@]}" || recovery_ok=false
      else
        mv -- "${MIGRATION_SIGNAL_BACKUP}" "${MIGRATION_SIGNAL_TARGET}" \
          || recovery_ok=false
        if [[ "${recovery_ok}" == true ]]; then
          migration_restore_data_root_identity || recovery_ok=false
        fi
        if [[ "${recovery_ok}" == true ]]; then
          migration_restart_stack \
            "${MIGRATION_SIGNAL_TARGET}" "${MIGRATION_SIGNAL_STACK_MODE}" \
            || recovery_ok=false
        fi
      fi
    else
      migration_restart_stack \
        "${MIGRATION_SIGNAL_TARGET}" "${MIGRATION_SIGNAL_STACK_MODE}" \
        || recovery_ok=false
    fi
    if [[ "${recovery_ok}" == true ]]; then
      migration_clear_journal || true
    fi
  fi
  exit "${signal_status}"
}

migration_rollback_switch() {
  local legacy_root="$1"
  local backup_root="$2"
  local failed_root="$3"
  local old_stack_mode="$4"
  local new_source_active="$5"
  shift 5
  local moved_items=("$@")
  local index
  local item
  local rollback_ok=true

  if [[ "${new_source_active}" == true ]]; then
    if [[ "${old_stack_mode}" == systemd* ]]; then
      systemctl stop thuyet-minh-offline.service >/dev/null 2>&1 || {
        migration_error "Không thể dừng systemd stack source mới trước rollback"
        return 1
      }
    elif [[ -x "${legacy_root}/scripts/native-stack.sh" ]]; then
      "${legacy_root}/scripts/native-stack.sh" stop >/dev/null 2>&1 || {
        migration_error "Không thể dừng stack source mới trước rollback"
        return 1
      }
    else
      migration_error "Không thể dừng stack source mới trước rollback"
      return 1
    fi
  fi

  for ((index=${#moved_items[@]} - 1; index >= 0; index--)); do
    item="${moved_items[index]}"
    if migration_path_exists "${legacy_root}/${item}" \
      && ! migration_path_exists "${backup_root}/${item}"; then
      mv -- "${legacy_root}/${item}" "${backup_root}/${item}" || rollback_ok=false
    else
      rollback_ok=false
    fi
  done

  if [[ "${rollback_ok}" != true ]]; then
    migration_error "Không thể tự rollback dữ liệu; không di chuyển thêm. Source mới: ${legacy_root}; backup: ${backup_root}"
    return 1
  fi

  if [[ "${new_source_active}" == true ]]; then
    if migration_path_exists "${failed_root}" || ! mv -- "${legacy_root}" "${failed_root}"; then
      migration_error "Không thể cất source mới để rollback; backup còn tại ${backup_root}"
      return 1
    fi
  fi
  if ! mv -- "${backup_root}" "${legacy_root}"; then
    migration_error "Không thể phục hồi source cũ từ ${backup_root}"
    return 1
  fi
  if ! migration_restore_data_root_identity; then
    migration_error "Source cũ đã phục hồi nhưng metadata data root chưa phục hồi"
    migration_write_journal rollback_incomplete "${moved_items[@]}" || true
    return 1
  fi
  if ! migration_restart_stack "${legacy_root}" "${old_stack_mode}"; then
    migration_error "Source và dữ liệu cũ đã phục hồi nhưng stack chưa khỏe"
    migration_write_journal rollback_incomplete "${moved_items[@]}" || true
    return 1
  fi
  migration_error "Đã rollback source, dữ liệu và stack cũ; source mới lỗi được giữ tại ${failed_root}"
  return 0
}

migrate_legacy_install() {
  local legacy_root="${1%/}"
  local staged_root="${2%/}"
  local requested_data_dir="${3:-${legacy_root}/var}"
  local data_dir_explicit="${4:-false}"
  local source_mode="${5:-legacy}"
  local expected_cuda_architecture="${6:-}"
  local expected_cuda_toolkit_version="${7:-}"
  local legacy_parent
  local staged_parent
  local configured_data_dir=""
  local effective_data_dir
  local backup_root
  local failed_root
  local timestamp
  local legacy_fingerprint
  local staged_fingerprint
  local legacy_tree_role
  local stack_mode="none"
  local stop_output=""
  local native_status_output=""
  local native_status=0
  local database_path
  local active_jobs
  local installed_cuda_architecture
  local installed_cuda_toolkit_version
  local new_source_active=false
  local item
  local persistent_items=(.venv-native)
  local moved_items=()

  MIGRATION_BACKUP_PATH=""
  MIGRATION_FAILED_SOURCE_PATH=""
  MIGRATION_EFFECTIVE_DATA_DIR=""
  MIGRATION_OLD_STACK_MODE="none"
  MIGRATION_MOVED_ITEMS=()
  MIGRATED_RUNTIME_REUSABLE=false
  MIGRATION_JOURNAL_PATH="${legacy_root}.migration-state.json"
  MIGRATION_DATA_ROOT_DEVICE=""
  MIGRATION_DATA_ROOT_INODE=""
  MIGRATION_DATA_ROOT_UID=""
  MIGRATION_DATA_ROOT_GID=""
  MIGRATION_DATA_ROOT_MODE=""

  [[ "${legacy_root}" == /* && "${staged_root}" == /* \
    && "${requested_data_dir}" == /* ]] || {
    migration_error "Đường dẫn migration và dữ liệu phải là tuyệt đối"
    return 1
  }
  case "${legacy_root}" in
    /|/workspace|/opt)
      migration_error "Từ chối migration thư mục hệ thống rộng: ${legacy_root}"
      return 1
      ;;
  esac
  [[ -d "${legacy_root}" && ! -L "${legacy_root}" \
    && "$(readlink -f -- "${legacy_root}")" == "${legacy_root}" ]] || {
    migration_error "Deployment cũ không phải đường dẫn thư mục chuẩn, thực"
    return 1
  }
  [[ -d "${staged_root}" && ! -L "${staged_root}" \
    && "$(readlink -f -- "${staged_root}")" == "${staged_root}" ]] || {
    migration_error "Source staging không phải đường dẫn thư mục chuẩn, thực"
    return 1
  }
  legacy_parent="$(readlink -f -- "$(dirname -- "${legacy_root}")")"
  staged_parent="$(readlink -f -- "$(dirname -- "${staged_root}")")"
  [[ "${legacy_parent}" == "${staged_parent}" \
    && "$(stat -c '%d' -- "${legacy_root}")" == "$(stat -c '%d' -- "${staged_root}")" ]] || {
    migration_error "Source staging phải ở cùng filesystem cha để đổi tên atomic"
    return 1
  }
  case "${source_mode}" in
    legacy)
      if migration_path_exists "${legacy_root}/.git" \
        || git -C "${legacy_root}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        migration_error "Deployment đích đã là Git worktree; hãy dùng --upgrade-existing"
        return 1
      fi
      legacy_tree_role=legacy
      ;;
    git)
      if ! migration_validate_git_tree "${legacy_root}" \
        || ! migration_validate_git_tree "${staged_root}"; then
        migration_error "Source Git không sạch hoặc không hợp lệ"
        return 1
      fi
      legacy_tree_role=staged
      ;;
    *)
      migration_error "Chế độ source migration không hợp lệ: ${source_mode}"
      return 1
      ;;
  esac
  migration_validate_project_tree "${legacy_root}" "${legacy_tree_role}" || {
    migration_error "Không nhận diện được deployment cũ"
    return 1
  }
  migration_validate_project_tree "${staged_root}" staged || {
    migration_error "Source mới không vượt qua kiểm tra marker"
    return 1
  }

  for item in .env.native var .venv-native; do
    if migration_path_exists "${staged_root}/${item}"; then
      migration_error "Source mới chứa đường dẫn persistent ngoài dự kiến: ${item}"
      return 1
    fi
  done
  if migration_path_exists "${legacy_root}/.env.native"; then
    [[ -f "${legacy_root}/.env.native" && ! -L "${legacy_root}/.env.native" ]] || {
      migration_error ".env.native phải là regular file, không phải symlink"
      return 1
    }
    [[ "$(stat -c '%s' -- "${legacy_root}/.env.native")" -le 1048576 ]] || {
      migration_error ".env.native lớn bất thường"
      return 1
    }
    configured_data_dir="$(migration_read_configured_data_root "${legacy_root}/.env.native")" || {
      migration_error "Không thể đọc DUB_NATIVE_ROOT an toàn từ .env.native"
      return 1
    }
    cp --preserve=mode,ownership,timestamps -- \
      "${legacy_root}/.env.native" "${staged_root}/.env.native" || return 1
    chmod 0600 "${staged_root}/.env.native" || return 1
    cmp -s -- "${legacy_root}/.env.native" "${staged_root}/.env.native" || {
      migration_error "Không sao chép nguyên vẹn .env.native"
      return 1
    }
  fi

  if [[ -n "${configured_data_dir}" ]]; then
    if [[ "${data_dir_explicit}" == true \
      && "${requested_data_dir}" != "${configured_data_dir}" ]]; then
      migration_error "--data-dir không khớp DUB_NATIVE_ROOT hiện có"
      return 1
    fi
    effective_data_dir="${configured_data_dir}"
  else
    effective_data_dir="${requested_data_dir}"
    if [[ "${data_dir_explicit}" == true \
      && -f "${staged_root}/.env.native" ]]; then
      printf '\nDUB_NATIVE_ROOT=%s\n' "${effective_data_dir}" \
        >>"${staged_root}/.env.native" || return 1
    fi
  fi
  if [[ "${effective_data_dir}" == "${legacy_root}/var" ]]; then
    persistent_items=(var .venv-native)
  elif [[ "${effective_data_dir}" == "${legacy_root}" \
    || "${effective_data_dir}" == "${legacy_root}/"* ]]; then
    migration_error "Chỉ tự động chuyển data nội bộ tại ${legacy_root}/var; hãy dùng data path ngoài source"
    return 1
  fi
  migration_capture_data_root_identity "${effective_data_dir}" || {
    migration_error "Data root phải là thư mục thực có metadata đọc được"
    return 1
  }
  if [[ -n "${expected_cuda_architecture}" ]]; then
    installed_cuda_architecture="$(
      migration_installed_cuda_architecture "${legacy_root}" "${effective_data_dir}"
    )" || {
      migration_error "Không xác định được CUDA architecture của runtime đang cài"
      return 1
    }
    if [[ "${installed_cuda_architecture}" != "${expected_cuda_architecture}" ]]; then
      migration_error "Runtime ${installed_cuda_architecture} không dùng được trên GPU ${expected_cuda_architecture}; cần cài mới để build lại native artifact"
      return 1
    fi
  fi
  if [[ -n "${expected_cuda_toolkit_version}" ]]; then
    installed_cuda_toolkit_version="$(
      migration_installed_cuda_toolkit_version \
        "${legacy_root}" "${effective_data_dir}"
    )" || {
      migration_error "Không xác định được CUDA toolkit của runtime đang cài"
      return 1
    }
    if [[ "${installed_cuda_toolkit_version}" != "${expected_cuda_toolkit_version}" ]]; then
      migration_error "Runtime CUDA ${installed_cuda_toolkit_version} không khớp toolkit ${expected_cuda_toolkit_version}; cần cài mới để build lại native artifact"
      return 1
    fi
  fi

  if command -v mountpoint >/dev/null; then
    if mountpoint -q -- "${legacy_root}"; then
      migration_error "Deployment root là mountpoint; không tự động rename"
      return 1
    fi
    for item in "${persistent_items[@]}"; do
      if [[ -d "${legacy_root}/${item}" && ! -L "${legacy_root}/${item}" ]] \
        && mountpoint -q -- "${legacy_root}/${item}"; then
        migration_error "${legacy_root}/${item} là mountpoint; không tự động migration"
        return 1
      fi
    done
  fi

  chmod --reference="${legacy_root}" -- "${staged_root}" || {
    migration_error "Không thể giữ quyền truy cập của project root"
    return 1
  }
  if [[ "$(stat -c '%u:%g' -- "${legacy_root}")" \
    != "$(stat -c '%u:%g' -- "${staged_root}")" ]]; then
    chown --reference="${legacy_root}" -- "${staged_root}" || {
      migration_error "Không thể giữ owner của project root"
      return 1
    }
  fi

  if [[ "${source_mode}" == git ]]; then
    legacy_fingerprint="$(
      migration_runtime_compatibility_fingerprint "${legacy_root}"
    )" || {
      migration_error "Không tạo được compatibility fingerprint source cũ"
      return 1
    }
    staged_fingerprint="$(
      migration_runtime_compatibility_fingerprint "${staged_root}"
    )" || {
      migration_error "Không tạo được compatibility fingerprint source mới"
      return 1
    }
  else
    legacy_fingerprint="$(migration_runtime_fingerprint "${legacy_root}")" || {
      migration_error "Không tạo được fingerprint source cũ"
      return 1
    }
    staged_fingerprint="$(migration_runtime_fingerprint "${staged_root}")" || {
      migration_error "Không tạo được fingerprint source mới"
      return 1
    }
  fi
  # The installer intentionally refuses to rebuild a persistent runtime after
  # the source switch because rollback cannot undo mutations inside that venv.
  # Reject an incompatible runtime here, before the journal or stack changes.
  if [[ "${legacy_fingerprint}" == "${staged_fingerprint}" \
    && -x "${legacy_root}/.venv-native/bin/python" ]]; then
    MIGRATED_RUNTIME_REUSABLE=true
  else
    migration_error "Runtime legacy không tương thích source mới; chưa dừng stack hoặc đổi source"
    return 1
  fi
  if migration_path_exists "${MIGRATION_JOURNAL_PATH}"; then
    migration_error "Đã có journal migration dang dở: ${MIGRATION_JOURNAL_PATH}"
    return 1
  fi

  if command -v systemctl >/dev/null \
    && systemctl is-active --quiet thuyet-minh-offline.service; then
    stack_mode="systemd"
    migration_wait_for_stack_health "${legacy_root}" || {
      migration_error "Systemd service cũ đang active nhưng stack không khỏe"
      return 1
    }
  else
    native_status_output="$("${legacy_root}/scripts/native-stack.sh" status 2>&1)" \
      || native_status=$?
    if [[ "${native_status}" -eq 0 ]]; then
      stack_mode="native"
      migration_stack_status_text_healthy "${native_status_output}" || {
        migration_error "Supervisor cũ đang chạy nhưng stack không đủ bốn service khỏe"
        return 1
      }
    elif [[ "${native_status}" -eq 1 ]]; then
      if migration_native_supervisor_present "${legacy_root}" "${effective_data_dir}"; then
        migration_error "Supervisor còn tồn tại dù status báo stack chưa chạy"
        return 1
      fi
      if command -v systemctl >/dev/null \
        && systemctl cat thuyet-minh-offline.service >/dev/null 2>&1; then
        stack_mode="systemd-stopped"
      fi
    else
      migration_error "Không đọc được trạng thái native stack cũ (exit ${native_status})"
      return 1
    fi
  fi

  timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
  backup_root="${legacy_root}.legacy-backup-${timestamp}-$$"
  failed_root="${legacy_root}.failed-migration-${timestamp}-$$"
  if migration_path_exists "${backup_root}" || migration_path_exists "${failed_root}"; then
    migration_error "Đường dẫn backup đã tồn tại; chưa thay đổi source"
    return 1
  fi

  MIGRATION_SIGNAL_TARGET="${legacy_root}"
  MIGRATION_SIGNAL_BACKUP="${backup_root}"
  MIGRATION_SIGNAL_FAILED="${failed_root}"
  MIGRATION_SIGNAL_STACK_MODE="${stack_mode}"
  MIGRATION_SIGNAL_EXPECTED_ITEMS=("${persistent_items[@]}")
  MIGRATION_EFFECTIVE_DATA_DIR="${effective_data_dir}"
  MIGRATION_SIGNAL_ARMED=true
  trap 'migration_abort_on_signal 130' INT
  trap 'migration_abort_on_signal 143' TERM HUP
  if ! migration_write_journal prepared; then
    MIGRATION_SIGNAL_ARMED=false
    trap - INT TERM HUP
    migration_clear_journal || true
    migration_error "Không thể ghi journal migration; chưa dừng stack hoặc đổi source"
    return 1
  fi

  case "${stack_mode}" in
    systemd)
      migration_log "Dừng systemd service cũ trước khi đổi source"
      systemctl stop thuyet-minh-offline.service || {
        migration_recover_before_source_switch "${legacy_root}" "${stack_mode}" \
          || true
        MIGRATION_SIGNAL_ARMED=false
        trap - INT TERM HUP
        migration_error "Không thể dừng systemd service cũ"
        return 1
      }
      ;;
    native)
      stop_output="$("${legacy_root}/scripts/native-stack.sh" stop 2>&1)" || {
        migration_recover_before_source_switch "${legacy_root}" "${stack_mode}" \
          || true
        MIGRATION_SIGNAL_ARMED=false
        trap - INT TERM HUP
        migration_error "Không thể chứng minh stack cũ đã dừng sạch"
        return 1
      }
      [[ -z "${stop_output}" ]] || printf '%s\n' "${stop_output}"
      ;;
    none) ;;
  esac
  if ! migration_write_journal stack_stopped; then
    migration_recover_before_source_switch "${legacy_root}" "${stack_mode}" \
      || true
    MIGRATION_SIGNAL_ARMED=false
    trap - INT TERM HUP
    migration_error "Không thể cập nhật journal sau khi dừng stack"
    return 1
  fi

  database_path="${effective_data_dir}/state/jobs.sqlite3"
  if [[ -f "${database_path}" ]]; then
    active_jobs="$(sqlite3 -readonly "${database_path}" \
      "SELECT count(*) FROM jobs WHERE active_slot = 1 OR status = 'cancelling';")" || {
      migration_recover_before_source_switch "${legacy_root}" "${stack_mode}" \
        || true
      MIGRATION_SIGNAL_ARMED=false
      trap - INT TERM HUP
      migration_error "Không kiểm tra được job đang hoạt động; chưa thay đổi source"
      return 1
    }
    if [[ ! "${active_jobs}" =~ ^[0-9]+$ || "${active_jobs}" -ne 0 ]]; then
      migration_recover_before_source_switch "${legacy_root}" "${stack_mode}" \
        || true
      MIGRATION_SIGNAL_ARMED=false
      trap - INT TERM HUP
      migration_error "Còn job đang hoạt động; đã dừng migration"
      return 1
    fi
  fi

  if ! mv -- "${legacy_root}" "${backup_root}"; then
    MIGRATION_SIGNAL_ARMED=false
    trap - INT TERM HUP
    migration_error "Không thể tạo backup source cũ"
    migration_recover_before_source_switch "${legacy_root}" "${stack_mode}" \
      || true
    return 1
  fi
  if ! migration_write_journal old_moved; then
    local old_restore_ok=true
    mv -- "${backup_root}" "${legacy_root}" || old_restore_ok=false
    MIGRATION_SIGNAL_ARMED=false
    trap - INT TERM HUP
    if [[ "${old_restore_ok}" == true ]]; then
      if migration_restore_data_root_identity; then
        migration_recover_before_source_switch "${legacy_root}" "${stack_mode}" \
          || true
      else
        migration_write_journal rollback_incomplete || true
      fi
    fi
    migration_error "Không cập nhật được journal sau backup; đã rollback"
    return 1
  fi
  if ! mv -- "${staged_root}" "${legacy_root}"; then
    migration_error "Không thể kích hoạt source mới; đang rollback"
    mv -- "${backup_root}" "${legacy_root}" || {
      migration_error "Cần phục hồi thủ công từ ${backup_root}"
      return 1
    }
    MIGRATION_SIGNAL_ARMED=false
    trap - INT TERM HUP
    if migration_restore_data_root_identity; then
      migration_recover_before_source_switch "${legacy_root}" "${stack_mode}" \
        || true
    else
      migration_write_journal rollback_incomplete || true
    fi
    return 1
  fi
  new_source_active=true
  if ! migration_write_journal new_active; then
    if migration_rollback_switch \
      "${legacy_root}" "${backup_root}" "${failed_root}" \
      "${stack_mode}" true; then
      migration_clear_journal || true
    fi
    MIGRATION_SIGNAL_ARMED=false
    trap - INT TERM HUP
    migration_error "Không cập nhật được journal sau source swap; đã rollback"
    return 1
  fi

  for item in "${persistent_items[@]}"; do
    if migration_path_exists "${backup_root}/${item}"; then
      if migration_path_exists "${legacy_root}/${item}" \
        || ! mv -- "${backup_root}/${item}" "${legacy_root}/${item}"; then
        migration_error "Không thể gắn lại ${item}; đang rollback"
        if migration_rollback_switch \
          "${legacy_root}" "${backup_root}" "${failed_root}" \
          "${stack_mode}" "${new_source_active}" "${moved_items[@]}"; then
          migration_clear_journal || true
        fi
        MIGRATION_SIGNAL_ARMED=false
        trap - INT TERM HUP
        return 1
      fi
      moved_items+=("${item}")
      if ! migration_write_journal persistent_moved "${moved_items[@]}"; then
        if migration_rollback_switch \
          "${legacy_root}" "${backup_root}" "${failed_root}" \
          "${stack_mode}" true "${moved_items[@]}"; then
          migration_clear_journal || true
        fi
        MIGRATION_SIGNAL_ARMED=false
        trap - INT TERM HUP
        migration_error "Không cập nhật được journal dữ liệu; đã rollback"
        return 1
      fi
    fi
  done

  MIGRATION_BACKUP_PATH="${backup_root}"
  MIGRATION_FAILED_SOURCE_PATH="${failed_root}"
  MIGRATION_OLD_STACK_MODE="${stack_mode}"
  MIGRATION_MOVED_ITEMS=("${moved_items[@]}")
  migration_log "Migration source đã chuẩn bị; backup không bị tự động xóa: ${backup_root}"
  return 0
}

#!/usr/bin/env bash
# VietDub — cài đặt bằng một lệnh (Ubuntu 22.04/24.04 + NVIDIA GPU).
#
# Cách dùng nhanh nhất, từ máy trống (chỉ cần curl + sudo):
#   curl -fsSL https://raw.githubusercontent.com/ngucungcode/vietdub/main/quick-install.sh | sudo bash
#   curl -fsSL https://raw.githubusercontent.com/ngucungcode/vietdub/main/quick-install.sh | sudo bash -s -- --profile minimal
#
# Hoặc từ source đã clone:
#   ./quick-install.sh [--profile auto|maximum|balanced|minimal] [--acceptance basic|full|none] [--no-start]
#
# Script này chỉ làm 3 việc, không tải gì khác ngoài source vietdub:
#   1. Kiểm tra Ubuntu + git (thiếu git thì tự cài qua apt).
#   2. Clone (hoặc tái dùng) source vietdub vào thư mục --dir.
#   3. Chạy install.sh của repo với --yes, rồi in địa chỉ dashboard.
# Mọi kiểm tra nặng (Python, CUDA, driver, GPU, model) do install.sh lo.
# Đọc kỹ script trước khi pipe qua bash; bản clone-then-run ở trên là
# lựa chọn an toàn hơn nếu bạn không muốn curl | bash.
#
# Biến môi trường (thay cho cờ tương ứng):
#   VIETDUB_QUICK_PROFILE, VIETDUB_QUICK_ACCEPTANCE,
#   VIETDUB_QUICK_BRANCH, VIETDUB_QUICK_DIR, VIETDUB_QUICK_REPO

set -Eeuo pipefail

REPO_URL="${VIETDUB_QUICK_REPO:-https://github.com/ngucungcode/vietdub.git}"
RAW_BASE="https://raw.githubusercontent.com/ngucungcode/vietdub/main"
PROFILE="${VIETDUB_QUICK_PROFILE:-auto}"
ACCEPTANCE="${VIETDUB_QUICK_ACCEPTANCE:-basic}"
BRANCH="${VIETDUB_QUICK_BRANCH:-main}"
CLONE_DIR="${VIETDUB_QUICK_DIR:-}"
START_STACK=true
DRY_RUN=false
FORWARD_ARGS=()

log() { printf '[vietdub] %s\n' "$*"; }
die() { printf '[vietdub] Lỗi: %s\n' "$*" >&2; exit 2; }

usage() {
  cat <<'EOF'
Cài VietDub bằng một lệnh trên Ubuntu 22.04/24.04 + NVIDIA GPU.

Cách dùng:
  curl -fsSL https://raw.githubusercontent.com/ngucungcode/vietdub/main/quick-install.sh | sudo bash
  curl -fsSL https://raw.githubusercontent.com/ngucungcode/vietdub/main/quick-install.sh | sudo bash -s -- [tùy chọn]
  ./quick-install.sh [tùy chọn]

Tùy chọn:
  --profile PROFILE      auto, maximum, balanced, minimal (mặc định: auto;
                         máy 6 GB VRAM nên dùng minimal, xem docs/gpu-6gb-separation-tts.md)
  --acceptance MODE      basic, full hoặc none (mặc định: basic)
  --branch REF           branch/tag/commit để clone (mặc định: main)
  --dir PATH             thư mục chứa source vietdub (mặc định: thư mục
                         của script nếu chạy từ source đã clone, còn lại ~/vietdub)
  --no-start             Cài xong nhưng không khởi động stack
  --dry-run              Chỉ in kế hoạch, không clone, không cài
  --help                 Hiển thị trợ giúp này
  Các cờ khác được chuyển nguyên vẹn sang install.sh (ví dụ --install-dir).
EOF
}

while (($#)); do
  case "$1" in
    --profile)    [[ $# -ge 2 ]] || die "--profile cần một giá trị"; PROFILE="$2"; shift 2 ;;
    --acceptance) [[ $# -ge 2 ]] || die "--acceptance cần một giá trị"; ACCEPTANCE="$2"; shift 2 ;;
    --branch)     [[ $# -ge 2 ]] || die "--branch cần một giá trị"; BRANCH="$2"; shift 2 ;;
    --dir)        [[ $# -ge 2 ]] || die "--dir cần một đường dẫn"; CLONE_DIR="$2"; shift 2 ;;
    --no-start)   START_STACK=false; shift ;;
    --dry-run)    DRY_RUN=true; shift ;;
    --help|-h)    usage; exit 0 ;;
    *)            FORWARD_ARGS+=("$1"); shift ;;
  esac
done

case "${PROFILE}" in
  auto|maximum|balanced|minimal) ;;
  *) die "--profile không hợp lệ: ${PROFILE} (chọn auto, maximum, balanced hoặc minimal)" ;;
esac
case "${ACCEPTANCE}" in
  basic|full|none) ;;
  *) die "--acceptance không hợp lệ: ${ACCEPTANCE} (chọn basic, full hoặc none)" ;;
esac

INSTALLER_ARGS=(--profile "${PROFILE}" --acceptance "${ACCEPTANCE}" --yes)
[[ "${START_STACK}" == true ]] || INSTALLER_ARGS+=(--no-start)
INSTALLER_ARGS+=("${FORWARD_ARGS[@]}")

# Thư mục source: --dir (hoặc VIETDUB_QUICK_DIR) thắng; nếu chạy từ file
# trong một source đã clone thì tái dùng chính thư mục đó; còn lại (kể
# cả khi pipe qua bash) mặc định ~/vietdub.
if [[ -z "${CLONE_DIR}" ]]; then
  if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
    SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
    if [[ -x "${SCRIPT_DIR}/install.sh" ]]; then
      CLONE_DIR="${SCRIPT_DIR}"
    fi
  fi
  [[ -n "${CLONE_DIR}" ]] || CLONE_DIR="$HOME/vietdub"
fi

if [[ "${DRY_RUN}" == true ]]; then
  log "Chế độ dry-run: không clone, không cài."
  log "Source: ${REPO_URL} (branch ${BRANCH}) -> ${CLONE_DIR}"
  log "Lệnh sẽ chạy: ${CLONE_DIR}/install.sh ${INSTALLER_ARGS[*]}"
  log "Sau khi cài: mở http://127.0.0.1:8080/ (trừ khi dùng --no-start)"
  exit 0
fi

[[ "$(uname -s)" == "Linux" ]] || die "quick-install.sh chỉ hỗ trợ Linux (Ubuntu 22.04/24.04)."
if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  case "${ID:-} ${VERSION_ID:-}" in
    "ubuntu 22.04"|"ubuntu 24.04") ;;
    *) die "Chỉ hỗ trợ Ubuntu 22.04 hoặc 24.04 (máy này: ${ID:-?} ${VERSION_ID:-?})." ;;
  esac
else
  die "Không xác định được hệ điều hành (thiếu /etc/os-release)."
fi

if ! command -v nvidia-smi >/dev/null 2>&1; then
  log "Cảnh báo: chưa thấy nvidia-smi; install.sh sẽ kiểm tra GPU/CUDA và dừng lại nếu không đạt."
fi

if ! command -v git >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then
    log "Chưa có git, đang cài qua apt..."
    if [[ "$(id -u)" -eq 0 ]]; then
      apt-get update && apt-get install -y git curl ca-certificates
    else
      command -v sudo >/dev/null 2>&1 || die "Cần git nhưng không có sudo để cài; hãy cài git rồi chạy lại."
      sudo apt-get update && sudo apt-get install -y git curl ca-certificates
    fi
  else
    die "Chưa có git và không có apt-get; hãy cài git rồi chạy lại."
  fi
fi

if [[ -x "${CLONE_DIR}/install.sh" ]]; then
  log "Tái dùng source sẵn có tại ${CLONE_DIR} (không clone lại)."
elif [[ -e "${CLONE_DIR}" ]]; then
  die "Thư mục ${CLONE_DIR} đã tồn tại nhưng không phải source vietdub (thiếu install.sh); hãy dọn hoặc chọn --dir khác."
else
  log "Đang clone ${REPO_URL} (branch ${BRANCH}) vào ${CLONE_DIR}..."
  git clone --branch "${BRANCH}" --depth 1 "${REPO_URL}" "${CLONE_DIR}"
fi

log "Đang chạy install.sh (tải model + acceptance ${ACCEPTANCE}; bước này tốn thời gian và băng thông)..."
if [[ "$(id -u)" -eq 0 ]]; then
  "${CLONE_DIR}/install.sh" "${INSTALLER_ARGS[@]}"
else
  command -v sudo >/dev/null 2>&1 || die "install.sh cần quyền root; hãy chạy lại bằng sudo."
  sudo "${CLONE_DIR}/install.sh" "${INSTALLER_ARGS[@]}"
fi

log "Cài đặt xong."
if [[ "${START_STACK}" == true ]]; then
  log "Mở dashboard: http://127.0.0.1:8080/"
  log "Kiểm tra nhanh: dub health"
else
  log "Stack chưa khởi động (--no-start); khởi động bằng: sudo systemctl start thuyet-minh-offline.service"
fi

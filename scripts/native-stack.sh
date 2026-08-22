#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/native-common.sh"

SUPERVISORD="${DUB_VENV_DIR}/bin/supervisord"
SUPERVISORCTL="${DUB_VENV_DIR}/bin/supervisorctl"
SUPERVISOR_CONFIG="${PROJECT_ROOT}/native/supervisord.conf"

require_runtime() {
  if [[ ! -x "${SUPERVISORD}" || ! -x "${DUB_PROWLARR_BIN}" ]]; then
    echo "Chưa bootstrap runtime native; hãy chạy scripts/native-bootstrap.sh" >&2
    exit 2
  fi
  install -d -m 0750 -o "${DUB_NATIVE_USER}" -g "${DUB_NATIVE_USER}" \
    "${DUB_RUNTIME_RUN_DIR}" "${DUB_RUNTIME_LOG_DIR}"
}

control() {
  "${SUPERVISORCTL}" -c "${SUPERVISOR_CONFIG}" "$@"
}

is_running() {
  control pid >/dev/null 2>&1
}

all_programs_running() {
  local status_output="$1"
  [[ "$(printf '%s\n' "${status_output}" | grep -c ' RUNNING ' || true)" -eq 4 ]] \
    && ! printf '%s\n' "${status_output}" | grep -Eq ' (BACKOFF|EXITED|FATAL|STOPPED|UNKNOWN) '
}

start_stack() {
  require_runtime
  if is_running; then
    local current_status
    current_status="$(control status || true)"
    printf '%s\n' "${current_status}"
    if all_programs_running "${current_status}"; then
      echo "Stack native đã chạy"
      return
    fi
    echo "Supervisor đang chạy nhưng stack chưa khỏe" >&2
    exit 1
  fi
  # Descriptor 9 belongs to the installer lock when this command is launched
  # during provider-mode installation. Never let the persistent daemon retain it.
  nohup "${SUPERVISORD}" -n -c "${SUPERVISOR_CONFIG}" 9>&- \
    >>"${DUB_RUNTIME_LOG_DIR}/launcher.log" 2>&1 </dev/null &
  local launcher_pid=$!
  for _ in {1..30}; do
    if is_running; then
      local current_status
      current_status="$(control status || true)"
      if all_programs_running "${current_status}"; then
        printf '%s\n' "${current_status}"
        return
      fi
    fi
    if ! kill -0 "${launcher_pid}" 2>/dev/null; then
      break
    fi
    sleep 1
  done
  echo "Không thể khởi động stack native" >&2
  tail -n 80 "${DUB_RUNTIME_LOG_DIR}/launcher.log" >&2 || true
  exit 1
}

stop_stack() {
  if ! is_running; then
    echo "Stack native chưa chạy"
    return
  fi
  local supervisor_pid
  local process_state
  supervisor_pid="$(control pid)"
  if [[ ! "${supervisor_pid}" =~ ^[0-9]+$ ]]; then
    echo "PID supervisor không hợp lệ" >&2
    exit 1
  fi
  control shutdown
  for _ in {1..30}; do
    if [[ ! -r "/proc/${supervisor_pid}/stat" ]]; then
      echo "Stack native đã dừng sạch"
      return
    fi
    process_state="$(awk '{print $3}' "/proc/${supervisor_pid}/stat")"
    if [[ "${process_state}" == "Z" ]]; then
      echo "Stack native đã dừng sạch"
      return
    fi
    sleep 1
  done
  echo "Stack chưa dừng sau 30 giây; không tự động SIGKILL" >&2
  exit 1
}

case "${1:-status}" in
  foreground)
    require_runtime
    exec "${SUPERVISORD}" -n -c "${SUPERVISOR_CONFIG}"
    ;;
  start)
    start_stack
    ;;
  stop)
    stop_stack
    ;;
  restart)
    stop_stack
    start_stack
    ;;
  status)
    require_runtime
    if is_running; then
      control status
    else
      echo "Stack native chưa chạy"
      exit 1
    fi
    ;;
  logs)
    require_runtime
    tail -n "${2:-100}" "${DUB_RUNTIME_LOG_DIR}"/*.log
    ;;
  *)
    echo "Cách dùng: $0 {foreground|start|stop|restart|status|logs [số_dòng]}" >&2
    exit 2
    ;;
esac

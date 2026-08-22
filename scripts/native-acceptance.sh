#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/native-common.sh"

SUPERVISORCTL="${DUB_VENV_DIR}/bin/supervisorctl"
SUPERVISOR_CONFIG="${PROJECT_ROOT}/native/supervisord.conf"

control() {
  "${SUPERVISORCTL}" -c "${SUPERVISOR_CONFIG}" "$@"
}

"${SCRIPT_DIR}/native-preflight.sh"

before_api="$(control pid api)"
before_worker="$(control pid worker)"
control signal TERM api worker >/dev/null

recovered=false
for _ in {1..30}; do
  sleep 1
  after_api="$(control pid api 2>/dev/null || true)"
  after_worker="$(control pid worker 2>/dev/null || true)"
  recovery_status="$(control status api worker 2>/dev/null || true)"
  if [[ "${after_api}" =~ ^[0-9]+$ \
    && "${after_worker}" =~ ^[0-9]+$ \
    && "${before_api}" != "${after_api}" \
    && "${before_worker}" != "${after_worker}" \
    && "$(printf '%s\n' "${recovery_status}" | grep -c ' RUNNING ' || true)" -eq 2 ]]; then
    recovered=true
    break
  fi
done
if [[ "${recovered}" != true ]]; then
  echo "API/worker không tự phục hồi sau SIGTERM" >&2
  exit 1
fi

for port in 8080 8081 9696; do
  addresses="$(ss -H -lnt "sport = :${port}" | awk '{print $4}')"
  if [[ -z "${addresses}" ]] || printf '%s\n' "${addresses}" | grep -Evq '^127\.0\.0\.1:'; then
    echo "Cổng ${port} không chỉ bind loopback" >&2
    exit 1
  fi
done

curl --silent --show-error --fail http://127.0.0.1:8080/v1/health \
  | jq -e '.status == "ok" and .gpu.ready == true and .acquisition_configured == true and .database.journal_mode == "wal"' \
  >/dev/null

runuser -u "${DUB_NATIVE_USER}" --preserve-environment -- \
  "${DUB_VENV_DIR}/bin/python" "${SCRIPT_DIR}/native-qbittorrent-smoke.py" >/dev/null

"${PROJECT_ROOT}/scripts/native-stack.sh" status
echo "Acceptance native đạt: GPU, API, acquisition, loopback và tự phục hồi"

#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/native-common.sh"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Khởi tạo dịch vụ native phải chạy bằng root" >&2
  exit 2
fi

ROTATE_SECRETS=false
if [[ "${1:-}" == "--rotate-secrets" ]]; then
  ROTATE_SECRETS=true
elif [[ -n "${1:-}" ]]; then
  echo "Cách dùng: $0 [--rotate-secrets]" >&2
  exit 2
fi

SUPERVISORCTL="${DUB_VENV_DIR}/bin/supervisorctl"
SUPERVISOR_CONFIG="${PROJECT_ROOT}/native/supervisord.conf"
QBIT_CONFIG_PATH="${DUB_QBITTORRENT_PROFILE}/qBittorrent/config/qBittorrent.conf"
PROWLARR_CONFIG_PATH="${DUB_PROWLARR_DATA_DIR}/config.xml"

control() {
  "${SUPERVISORCTL}" -c "${SUPERVISOR_CONFIG}" "$@"
}

if ! control pid >/dev/null 2>&1; then
  echo "Stack native chưa chạy; hãy chạy scripts/native-stack.sh start" >&2
  exit 2
fi

control stop api >/dev/null 2>&1 || true
control stop qbittorrent >/dev/null 2>&1 || true
control stop prowlarr >/dev/null 2>&1 || true

if [[ ! -f "${QBIT_CONFIG_PATH}" || ! -f "${PROWLARR_CONFIG_PATH}" ]]; then
  echo "Chưa có cấu hình lần đầu của qBittorrent/Prowlarr" >&2
  exit 1
fi

set_qbit_preference() {
  local key="$1"
  local value="$2"
  local temporary
  temporary="$(mktemp --tmpdir="$(dirname -- "${QBIT_CONFIG_PATH}")" .qbit-config.XXXXXX)"
  awk -v key="${key}" -v value="${value}" '
    function emit_value() {
      if (in_preferences && !found) {
        print key "=" value
        found = 1
      }
    }
    /^\[/ {
      emit_value()
      in_preferences = ($0 == "[Preferences]")
      if (in_preferences) {
        saw_preferences = 1
      }
      print
      next
    }
    in_preferences && index($0, key "=") == 1 {
      if (!found) {
        print key "=" value
        found = 1
      }
      next
    }
    { print }
    END {
      emit_value()
      if (!saw_preferences) {
        print ""
        print "[Preferences]"
        print key "=" value
      }
    }
  ' "${QBIT_CONFIG_PATH}" >"${temporary}"
  chown "${DUB_NATIVE_USER}:${DUB_NATIVE_USER}" "${temporary}"
  chmod 0640 "${temporary}"
  mv -f "${temporary}" "${QBIT_CONFIG_PATH}"
}

set_qbit_preference 'Downloads\SavePath' "${DUB_INCOMING_DIR}/"
set_qbit_preference 'WebUI\Address' '127.0.0.1'
set_qbit_preference 'WebUI\Port' '8081'

sed -i \
  -e 's#<BindAddress>[^<]*</BindAddress>#<BindAddress>127.0.0.1</BindAddress>#' \
  -e 's#<LaunchBrowser>[^<]*</LaunchBrowser>#<LaunchBrowser>False</LaunchBrowser>#' \
  "${PROWLARR_CONFIG_PATH}"

if [[ "${ROTATE_SECRETS}" == true ]]; then
  prowlarr_key="$(openssl rand -hex 16)"
  sed -i "s#<ApiKey>[^<]*</ApiKey>#<ApiKey>${prowlarr_key}</ApiKey>#" "${PROWLARR_CONFIG_PATH}"
else
  prowlarr_key="$(sed -n 's#.*<ApiKey>\([^<]*\)</ApiKey>.*#\1#p' "${PROWLARR_CONFIG_PATH}")"
fi
if [[ ! "${prowlarr_key}" =~ ^[0-9a-fA-F]{32,64}$ ]]; then
  echo "API key Prowlarr không hợp lệ" >&2
  exit 1
fi
install -m 0600 -o "${DUB_NATIVE_USER}" -g "${DUB_NATIVE_USER}" /dev/null "${DUB_PROWLARR_API_KEY_FILE}"
printf '%s\n' "${prowlarr_key}" >"${DUB_PROWLARR_API_KEY_FILE}"
chown "${DUB_NATIVE_USER}:${DUB_NATIVE_USER}" "${PROWLARR_CONFIG_PATH}"

control start prowlarr >/dev/null
control start qbittorrent >/dev/null

services_ready=false
for _ in {1..30}; do
  if curl --silent http://127.0.0.1:8081/api/v2/app/version >/dev/null \
    && curl --silent --fail http://127.0.0.1:9696/ping >/dev/null; then
    services_ready=true
    break
  fi
  sleep 1
done
if [[ "${services_ready}" != true ]]; then
  echo "qBittorrent/Prowlarr không sẵn sàng sau 30 giây" >&2
  exit 1
fi

cookie_file="$(mktemp "${DUB_RUNTIME_RUN_DIR}/qbit-cookie.XXXXXX")"
trap 'rm -f -- "${cookie_file}"' EXIT
if [[ -s "${DUB_QBITTORRENT_PASSWORD_FILE}" && "${ROTATE_SECRETS}" != true ]]; then
  current_username="${DUB_QBITTORRENT_USERNAME}"
  current_password="$(<"${DUB_QBITTORRENT_PASSWORD_FILE}")"
else
  current_username="admin"
  current_password="adminadmin"
fi
login_result="$(curl --silent --show-error --cookie-jar "${cookie_file}" \
  --data-urlencode "username=${current_username}" \
  --data-urlencode "password=${current_password}" \
  http://127.0.0.1:8081/api/v2/auth/login)"
if [[ "${login_result}" != "Ok." && "${login_result}" != "Ok" ]]; then
  echo "Không thể đăng nhập qBittorrent để hoàn tất cấu hình" >&2
  exit 1
fi

new_password="$(openssl rand -hex 24)"
preferences_json="$(printf '{"web_ui_username":"%s","web_ui_password":"%s","save_path":"%s/","web_ui_address":"127.0.0.1","web_ui_port":8081}' \
  "${DUB_QBITTORRENT_USERNAME}" "${new_password}" "${DUB_INCOMING_DIR}")"
curl --silent --show-error --fail --cookie "${cookie_file}" \
  --data-urlencode "json=${preferences_json}" \
  http://127.0.0.1:8081/api/v2/app/setPreferences >/dev/null

verify_result="$(curl --silent --show-error \
  --data-urlencode "username=${DUB_QBITTORRENT_USERNAME}" \
  --data-urlencode "password=${new_password}" \
  http://127.0.0.1:8081/api/v2/auth/login)"
if [[ "${verify_result}" != "Ok." && "${verify_result}" != "Ok" ]]; then
  echo "Không thể xác minh mật khẩu qBittorrent mới" >&2
  exit 1
fi
install -m 0600 -o "${DUB_NATIVE_USER}" -g "${DUB_NATIVE_USER}" /dev/null "${DUB_QBITTORRENT_PASSWORD_FILE}"
printf '%s\n' "${new_password}" >"${DUB_QBITTORRENT_PASSWORD_FILE}"

curl --silent --show-error --fail \
  -H "X-Api-Key: $(<"${DUB_PROWLARR_API_KEY_FILE}")" \
  http://127.0.0.1:9696/api/v1/system/status >/dev/null

control start api >/dev/null
echo "Đã cấu hình dịch vụ native và lưu secret cục bộ"

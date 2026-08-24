#!/usr/bin/env bash

set -Eeuo pipefail

if [[ "$#" -ne 1 ]]; then
  echo "Cach dung: $0 DATA_ROOT" >&2
  exit 2
fi

requested_root="${1%/}"
[[ "${requested_root}" == /* \
  && "${requested_root}" != *$'\n'* \
  && "${requested_root}" != *$'\r'* ]] || {
  echo "Data root phai la duong dan tuyet doi hop le" >&2
  exit 2
}
data_root="$(readlink -m -- "${requested_root}")"
[[ "${data_root}" != "/" && "${data_root}" == "${requested_root}" ]] || {
  echo "Data root phai la duong dan chuan hoa, khong phai /" >&2
  exit 2
}

if [[ -e "${data_root}" || -L "${data_root}" ]]; then
  [[ -d "${data_root}" && ! -L "${data_root}" ]] || {
    echo "Data root hien co phai la thu muc thuc, khong phai symlink" >&2
    exit 2
  }
  printf 'existing\n'
  exit 0
fi

# A new root must remain traversable until the runtime service account is
# created. install.sh narrows it to root:<service-group> 0750 afterwards.
install -d -m 0755 -- "${data_root}"
printf 'created\n'

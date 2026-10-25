#!/usr/bin/env bash
# Build file cai dat VietDub-Setup.exe tu windows/vietdub-setup.nsi.
# Can NSIS 3 (lenh makensis). Ubuntu: sudo apt-get install nsis
# Ket qua: dist/VietDub-Setup.exe (khong commit file .exe len repo).
set -Eeuo pipefail

cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
command -v makensis >/dev/null 2>&1 || {
  echo '[vietdub] Loi: thieu makensis. Ubuntu: sudo apt-get install nsis' >&2
  exit 2
}
mkdir -p ../dist
makensis vietdub-setup.nsi
ls -l ../dist/VietDub-Setup.exe
file ../dist/VietDub-Setup.exe

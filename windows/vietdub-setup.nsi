; VietDub Setup — trình cài đặt .exe cho Windows 10/11 x64.
;
; Biên dịch (cần NSIS 3, lệnh makensis):
;   ./windows/build-setup-exe.sh
; Kết quả: dist/VietDub-Setup.exe
;
; File .exe này KHÔNG đóng gói sẵn source vietdub. Khi chạy, nó:
;   1. Lấy windows/bootstrap.ps1 chính hãng: ưu tiên file bootstrap.ps1
;      đặt cạnh file .exe (hoặc trong thư mục windows/ kề bên), nếu
;      không có thì tải từ GitHub về %TEMP%.
;   2. Chạy bootstrap với -InstallRoot "$INSTDIR\source". Bootstrap sẽ
;      tải source .zip, cài prerequisite (tự xin quyền Administrator qua
;      UAC khi cần), build stack và mở dashboard.
; Cần mạng trong lúc cài. Dữ liệu người dùng (%LOCALAPPDATA%\VietDub)
; không bị xóa khi gỡ cài đặt.
;
; Lưu ý: file .nsi này lưu ở dạng UTF-8 có BOM để bộ cài Unicode hiển
; thị đúng tiếng Việt. Đừng lưu lại bằng encoding khác.

Unicode true
!include "MUI2.nsh"
!include "LogicLib.nsh"

!define APP_VERSION "0.3.8"
!define APP_NAME "VietDub"
!define BOOTSTRAP_URL "https://raw.githubusercontent.com/ngucungcode/vietdub/main/windows/bootstrap.ps1"

Name "${APP_NAME}"
OutFile "..\dist\VietDub-Setup.exe"
InstallDir "$LOCALAPPDATA\Programs\ThuyetMinhOfflineGPU"
RequestExecutionLevel admin
SetCompressor /SOLID lzma

VIProductVersion "${APP_VERSION}.0"
VIAddVersionKey "ProductName" "${APP_NAME}"
VIAddVersionKey "FileDescription" "Trinh cai dat ${APP_NAME}"
VIAddVersionKey "FileVersion" "${APP_VERSION}"
VIAddVersionKey "ProductVersion" "${APP_VERSION}"
VIAddVersionKey "LegalCopyright" "GPL-3.0-or-later"

!define MUI_ABORTWARNING
!define MUI_ICON "${NSISDIR}\Contrib\Graphics\Icons\modern-install.ico"
!define MUI_UNICON "${NSISDIR}\Contrib\Graphics\Icons\modern-uninstall.ico"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "..\LICENSE"
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "Vietnamese"

Section "${APP_NAME}" SEC_MAIN
  SetOutPath "$INSTDIR"

  DetailPrint "Dang chuan bi bootstrap..."
  ; $R1 = duong dan file bootstrap se chay.
  StrCpy $R1 "$TEMP\vietdub-bootstrap.ps1"
  ${If} ${FileExists} "$EXEDIR\bootstrap.ps1"
    DetailPrint "Dung bootstrap san co canh file .exe."
    CopyFiles /SILENT "$EXEDIR\bootstrap.ps1" "$TEMP\"
    StrCpy $R1 "$TEMP\bootstrap.ps1"
  ${ElseIf} ${FileExists} "$EXEDIR\..\windows\bootstrap.ps1"
    DetailPrint "Dung bootstrap san co trong thu muc windows/ ke ben."
    CopyFiles /SILENT "$EXEDIR\..\windows\bootstrap.ps1" "$TEMP\"
    StrCpy $R1 "$TEMP\bootstrap.ps1"
  ${Else}
    DetailPrint "Dang tai bootstrap chinh hang tu GitHub..."
    nsExec::ExecToLog `"$WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -Command "Invoke-WebRequest -UseBasicParsing '${BOOTSTRAP_URL}' -OutFile '$TEMP\vietdub-bootstrap.ps1'"`
    Pop $0
    ${If} $0 != "0"
      MessageBox MB_ICONSTOP "Khong tai duoc bootstrap.ps1 (ma loi: $0). Kiem tra mang roi chay lai file .exe nay."
      Abort
    ${EndIf}
  ${EndIf}
  ${If} ${FileExists} $R1
  ${Else}
    MessageBox MB_ICONSTOP "Khong tim thay file bootstrap de chay. Kiem tra mang roi chay lai file .exe nay."
    Abort
  ${EndIf}

  DetailPrint "Dang chay bootstrap (tai source, cai prerequisite, build stack — buoc nay ton thoi gian)..."
  nsExec::ExecToLog `"$WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "$R1" -InstallRoot "$INSTDIR\source"`
  Pop $0
  ${If} $0 != "0"
    MessageBox MB_ICONSTOP "Cai dat that bai (ma loi: $0). Neu may yeu cau khoi dong lai (sau khi cai VS Build Tools hoac prerequisite), hay khoi dong lai Windows roi chay lai file .exe nay."
    Abort
  ${EndIf}

  WriteUninstaller "$INSTDIR\Uninstall.exe"
  CreateDirectory "$SMPROGRAMS\${APP_NAME}"
  CreateShortcut "$SMPROGRAMS\${APP_NAME}\${APP_NAME} Dashboard.lnk" "http://127.0.0.1:8080/"
  CreateShortcut "$SMPROGRAMS\${APP_NAME}\Go cai dat ${APP_NAME}.lnk" "$INSTDIR\Uninstall.exe"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "DisplayName" "${APP_NAME}"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "DisplayVersion" "${APP_VERSION}"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "Publisher" "ngucungcode"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "UninstallString" "$INSTDIR\Uninstall.exe"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "InstallLocation" "$INSTDIR"
  WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "NoModify" 1
  WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "NoRepair" 1
  DetailPrint "Cai dat xong. Dashboard: http://127.0.0.1:8080/"
SectionEnd

Section "Uninstall"
  Delete "$SMPROGRAMS\${APP_NAME}\${APP_NAME} Dashboard.lnk"
  Delete "$SMPROGRAMS\${APP_NAME}\Go cai dat ${APP_NAME}.lnk"
  RMDir "$SMPROGRAMS\${APP_NAME}"
  DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}"
  ; Chi xoa thu muc cai dat (source + uninstaller). Du lieu nguoi dung
  ; trong %LOCALAPPDATA%\VietDub va cac prerequisite (Python, Git, ...)
  ; duoc giu lai co tinh.
  RMDir /r "$INSTDIR"
SectionEnd

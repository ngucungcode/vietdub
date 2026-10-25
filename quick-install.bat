@echo off
REM VietDub — cài đặt bằng một cú nhấp đúp (Windows 10/11 x64).
REM
REM Cách dùng:
REM   1. Nhấp đúp file này (từ source đã clone, hoặc file tải riêng).
REM   2. Bấm Yes khi Windows hỏi quyền Administrator (để cài prerequisite).
REM   3. Đợi cài xong, dashboard tự mở ở http://127.0.0.1:8080/
REM
REM File này chỉ làm 2 việc, không tải gì khác ngoài source vietdub:
REM   - Nếu chạy từ source đã clone (có windows\bootstrap.ps1) thì chạy nó.
REM   - Nếu chạy standalone thì tải windows\bootstrap.ps1 chính hãng từ
REM     GitHub về %TEMP% rồi chạy; chính bootstrap sẽ tải source .zip,
REM     cài prerequisite, build stack và mở dashboard.
REM Mọi kiểm tra nặng (Python, Git, CMake, Ninja, FFmpeg, VS Build Tools,
REM NVIDIA driver/CUDA, model) do bootstrap/installer lo.
REM Muốn đổi profile, chạy từ PowerShell thay vì nhấp đúp, ví dụ:
REM   .\quick-install.bat -Profile minimal
REM (tham số được chuyển nguyên vẹn sang windows\bootstrap.ps1)

setlocal
set "SCRIPT_DIR=%~dp0"
set "BOOTSTRAP=%SCRIPT_DIR%windows\bootstrap.ps1"

if exist "%BOOTSTRAP%" (
    echo [vietdub] Dung bootstrap san co: %BOOTSTRAP%
) else (
    echo [vietdub] Dang tai bootstrap chinh hang tu GitHub...
    set "BOOTSTRAP=%TEMP%\vietdub-bootstrap.ps1"
    powershell.exe -NoProfile -Command "Invoke-WebRequest -UseBasicParsing 'https://raw.githubusercontent.com/ngucungcode/vietdub/main/windows/bootstrap.ps1' -OutFile '%BOOTSTRAP%'"
    if errorlevel 1 (
        echo [vietdub] Loi: khong tai duoc bootstrap.ps1. Kiem tra mang roi chay lai.
        pause
        exit /b 1
    )
)

echo [vietdub] Dang chay bootstrap (buoc nay ton thoi gian, co the hoi UAC)...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%BOOTSTRAP%" %*
if errorlevel 1 (
    echo [vietdub] Cai dat gap loi. Chay lai file nay sau khi sua loi hien tren man hinh.
    pause
    exit /b 1
)

echo.
echo [vietdub] Cai dat xong. Dashboard: http://127.0.0.1:8080/
pause

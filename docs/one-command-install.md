# Cài đặt một lệnh / một cú nhấp

Bộ cài nhanh này bọc quanh installer chính của repo (`install.sh` trên
Ubuntu, `windows/bootstrap.ps1` trên Windows). Nó không thay thế installer
chính, chỉ bỏ qua các bước tay chân trước đó: khỏi cần clone trước, khỏi
cần nhớ cờ, khỏi cần mở PowerShell gõ lệnh dài.

## Ubuntu 22.04/24.04 + NVIDIA GPU: một lệnh

Từ máy trống (chỉ cần `curl` + `sudo`):

```bash
curl -fsSL https://raw.githubusercontent.com/ngucungcode/vietdub/main/quick-install.sh | sudo bash
```

Máy 6 GB VRAM (profile nhẹ, xem `docs/gpu-6gb-separation-tts.md`):

```bash
curl -fsSL https://raw.githubusercontent.com/ngucungcode/vietdub/main/quick-install.sh | sudo bash -s -- --profile minimal
```

Từ source đã clone:

```bash
./quick-install.sh
./quick-install.sh --profile minimal --acceptance none --no-start
```

### Lệnh đó làm gì, theo thứ tự

1. Kiểm tra hệ điều hành là Ubuntu 22.04 hoặc 24.04 (sai là dừng ngay
   với thông báo rõ, thay vì chạy nửa chừng mới lỗi).
2. Kiểm tra `nvidia-smi`; chưa thấy thì chỉ cảnh báo, `install.sh` sẽ
   kiểm tra GPU/CUDA chính thức sau đó.
3. Thiếu `git` thì tự cài qua `apt` (cần `sudo`).
4. Clone source vietdub vào `~/vietdub` (khi chạy qua pipe với `sudo`,
   thư mục này là `/root/vietdub`). Đã có source hợp lệ thì tái dùng,
   không clone lại; thư mục đã tồn tại nhưng không phải source vietdub
   thì dừng và yêu cầu chọn `--dir` khác.
5. Chạy `install.sh --profile auto --acceptance basic --yes` (hoặc đúng
   cờ bạn truyền), rồi in địa chỉ dashboard.

### Tùy chọn của `quick-install.sh`

| Cờ / biến môi trường | Mặc định | Ý nghĩa |
|---|---|---|
| `--profile`, `VIETDUB_QUICK_PROFILE` | `auto` | `auto`, `maximum`, `balanced`, `minimal` |
| `--acceptance`, `VIETDUB_QUICK_ACCEPTANCE` | `basic` | `basic`, `full`, `none` |
| `--branch`, `VIETDUB_QUICK_BRANCH` | `main` | branch/tag/commit để clone |
| `--dir`, `VIETDUB_QUICK_DIR` | thư mục script (nếu chạy từ source), còn lại `~/vietdub` | thư mục chứa source |
| `--no-start` | tắt | cài xong nhưng không khởi động stack |
| `--dry-run` | tắt | chỉ in kế hoạch, không clone, không cài |
| cờ khác | — | chuyển nguyên vẹn sang `install.sh` (ví dụ `--install-dir`) |

Cài xong, mở `http://127.0.0.1:8080/`, kiểm tra nhanh bằng `dub health`.
Nếu dùng `--no-start`, khởi động sau bằng
`sudo systemctl start thuyet-minh-offline.service`.

### Lưu ý an toàn với `curl | bash`

Lệnh pipe chạy code tải từ mạng. Trước khi chạy, bạn có thể đọc
`quick-install.sh` ngay trên GitHub (nó chỉ clone repo chính hãng rồi
chạy `install.sh` của repo, không tải gì khác). Không muốn pipe thì dùng
cách clone trước rồi chạy `./quick-install.sh` ở trên — kết quả như nhau.

## Windows 10/11 x64: một cú nhấp đúp

1. Lấy `quick-install.bat`: clone repo, hoặc tải file zip của repo từ
   GitHub rồi giải nén.
2. Nhấp đúp `quick-install.bat`.
3. Bấm **Yes** khi Windows hỏi quyền Administrator (để cài prerequisite).
4. Đợi cài xong; dashboard tự mở ở `http://127.0.0.1:8080/`.

Không có source sẵn mà vẫn muốn một lệnh duy nhất, dán vào PowerShell:

```powershell
Invoke-WebRequest -UseBasicParsing "https://raw.githubusercontent.com/ngucungcode/vietdub/main/quick-install.bat" -OutFile "$env:TEMP\quick-install.bat"; cmd /c "$env:TEMP\quick-install.bat"
```

`quick-install.bat` chỉ tìm `windows/bootstrap.ps1` trong source, hoặc
tải đúng file đó từ GitHub về `%TEMP%` rồi chạy. Muốn đổi profile khi
nhấp đúp là không đổi được — chạy từ PowerShell thay thế, ví dụ
`.\quick-install.bat -Profile minimal` (tham số chuyển sang bootstrap).
Bootstrap tự xin quyền Administrator qua UAC khi cần cài prerequisite
(Python, Git, CMake, Ninja, FFmpeg, VS Build Tools), nên bạn không cần
mở PowerShell với quyền admin trước.

## Windows: file cài `.exe` (không cần gõ lệnh)

`VietDub-Setup.exe` là trình cài đặt NSIS thật (giao diện wizard tiếng
Việt: chào mừng → giấy phép GPL → chọn thư mục → cài đặt). Cách chạy:

1. Nhấp đúp `VietDub-Setup.exe`.
2. Bấm **Yes** khi Windows hỏi quyền Administrator.
3. Chọn thư mục cài (mặc định
   `%LOCALAPPDATA%\Programs\ThuyetMinhOfflineGPU`, source sẽ nằm trong
   `source` bên dưới), bấm Install rồi đợi.
4. Cài xong, dashboard tự mở ở `http://127.0.0.1:8080/`; Start Menu có
   thêm shortcut "VietDub Dashboard" và "Gỡ cài đặt VietDub".

File `.exe` không đóng gói sẵn source: lúc cài, nó ưu tiên
`bootstrap.ps1` đặt cạnh file `.exe` (hoặc trong `windows/` kề bên),
không có thì tải bản chính hãng từ GitHub về `%TEMP%` rồi chạy với
`-InstallRoot` đúng thư mục bạn chọn. Cần mạng trong lúc cài. Gỡ cài đặt
chỉ xóa thư mục cài đặt (source + uninstaller); dữ liệu trong
`%LOCALAPPDATA%\VietDub` và các prerequisite (Python, Git, ...) được giữ
lại cố tình.

Tự build lại file `.exe` từ source (cần NSIS 3, lệnh `makensis`):

```bash
./windows/build-setup-exe.sh   # kết quả: dist/VietDub-Setup.exe
```

File `.exe` không được commit lên repo (thư mục `dist/` đã nằm trong
`.gitignore`); muốn phát hành thì gắn nó vào release trên GitHub.

Lưu ý: file `.exe` tự build chưa được ký số, nên Windows SmartScreen có
thể chặn lần đầu — bấm "More info" rồi "Run anyway" mới chạy được. Muốn
hết cảnh báo này thì cần chứng chỉ ký mã (code signing certificate).

## Hỏi đáp nhanh

- **Chạy lại được không?** Được. Chạy lại đúng lệnh cũ: source đã có thì
  tái dùng, `install.sh` tiếp tục từ trạng thái hiện tại. Muốn nâng cấp
  deployment cũ có chủ đích thì dùng cờ `--migrate-existing` /
  `--upgrade-existing` của `install.sh` (xem trợ giúp của nó).
- **Cài có nhanh không?** Một lệnh chỉ là một lần gõ; thời gian thật vẫn
  phụ thuộc tải model và build (băng thông + ổ đĩa), cộng acceptance
  `basic` mặc định. Muốn bỏ qua acceptance thì thêm `--acceptance none`
  (Ubuntu) — nhưng đừng công bố GPU/model là production nếu chưa chạy
  acceptance trên phần cứng thật.
- **Lỗi giữa chừng thì sao?** Đọc dòng lỗi cuối màn hình rồi chạy lại
  đúng lệnh; đừng chạy song song hai lần. Lỗi về GPU/driver/CUDA là do
  máy chưa đạt yêu cầu trong README, không phải do bộ cài nhanh.
- **Gỡ cài đặt?** Bộ cài nhanh không có lệnh gỡ; dừng service rồi xóa
  thư mục cài đặt và thư mục dữ liệu theo đường dẫn `install.sh` đã in
  khi cài.

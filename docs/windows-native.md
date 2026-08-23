# Windows 10 native

Tài liệu này cài VietDub trực tiếp trên Windows 10, không dùng
WSL2 hoặc Docker. Bản Windows hiện là MVP local-upload: dashboard, API, worker và
toàn bộ pipeline chạy native trên GPU được hỗ trợ hoặc CPU compatibility mode; người
dùng tải MP4/MKV và SRT từ trình duyệt.

## Yêu cầu

- Windows 10 22H2 x64, build 19045 trở lên.
- GPU mode: NVIDIA RTX 20, 30, 40 hoặc 50 có ít nhất 6 GiB VRAM.
- CPU mode: không cần CUDA; dùng được với GPU cũ/không hỗ trợ như MX250, nhưng ASR,
  dịch và đặc biệt TIGER-DnR sẽ rất chậm.
- RAM tối thiểu 16 GiB và dung lượng trống tối thiểu 25/35/55 GiB tương ứng profile
  `minimal`/`balanced`/`maximum`, chưa tính phim và output.

Không cần cài prerequisite phần mềm trước. Bootstrap tự nâng quyền qua một hộp thoại
UAC, cài hoặc repair WinGet theo quy trình Microsoft, rồi cài phần còn thiếu: Python
3.12 x64, Git, CMake, Ninja, FFmpeg và Visual Studio 2022 C++ Build Tools. Chỉ GPU
mode mới cài NVIDIA CUDA Toolkit 12.8; CPU mode bỏ qua hoàn toàn package
`Nvidia.CUDA` và dùng wheel PyTorch CPU. CUDA 12.6/12.8 tương thích đang có được giữ
lại. Trong GPU mode, cuDNN 9 lấy từ wheel PyTorch `cu128` và được kiểm tra bằng probe
GPU trước khi hoàn tất.

Nguồn tự động là [WinGet](https://learn.microsoft.com/windows/package-manager/winget/),
bootstrapper [Visual Studio 2022 Build Tools](https://aka.ms/vs/17/release/vs_BuildTools.exe)
và package `Nvidia.CUDA` 12.8. Các installer chạy silent nhưng Windows vẫn hiện UAC;
đây là bước xác nhận quyền quản trị không thể và không nên bỏ qua.

| Dòng GPU | CUDA target | Trạng thái Windows | Profile |
|---|---|---|---|
| RTX 20 | `sm_75` | Hỗ trợ | Theo VRAM |
| RTX 30 | `sm_86` | Hỗ trợ | Theo VRAM |
| RTX 40 | `sm_89` | Hỗ trợ | Theo VRAM |
| RTX 50 | `sm_120` | Thử nghiệm, cần CUDA 12.8 | Chỉ `minimal` |

`-ComputeMode auto` chọn GPU khi card nằm trong ma trận và có ít nhất 6 GiB VRAM;
nếu không, installer tự chuyển sang CPU. GPU mode chọn `minimal` từ 6 GiB,
`balanced` từ 8 GiB và `maximum` từ 22 GiB. CPU mode chỉ dùng profile `minimal`.
RTX 50 luôn bị khóa ở `minimal` cho đến khi có báo cáo nghiệm thu phần cứng đầy đủ.

## Cài đặt

Mở PowerShell thường và chạy một dòng. Lệnh tải bootstrap về file tạm rồi thực thi;
không dùng `Invoke-Expression`:

```powershell
$p=Join-Path $env:TEMP "thuyetminh-bootstrap.ps1"; Invoke-WebRequest -UseBasicParsing "https://raw.githubusercontent.com/ngucungcode/vietdub/main/windows/bootstrap.ps1" -OutFile $p; powershell.exe -NoProfile -ExecutionPolicy Bypass -File $p
```

Bootstrap tải source vào `%LOCALAPPDATA%\Programs\VietDub\source`, yêu
cầu UAC một lần, tự chọn CPU/GPU và profile, cài model, khởi động API + worker, chờ
health check và mở `http://127.0.0.1:8080/`. Nếu đã clone repository thì chỉ cần chạy
`.\windows\bootstrap.ps1` trong thư mục dự án.

Installer thực hiện các bước fail-closed sau:

1. Cài/repair prerequisite còn thiếu và làm mới `PATH` ngay trong tiến trình hiện tại.
2. Chọn compute mode; chỉ GPU mode mới kiểm tra driver, VRAM và CUDA target.
3. Build `llama.cpp` từ commit đã khóa với backend CPU hoặc đúng CUDA architecture.
4. Lấy TIGER và VieNeu đúng commit, rồi xác minh các file overlay bằng SHA-256.
5. Tạo `.venv-windows`, cài PyTorch 2.8.0 CPU hoặc `cu128` và dependency đã khóa.
6. Chạy phép nhân tensor, CTranslate2, worker preflight, cài model và khởi động stack.

Nếu Visual Studio hoặc NVIDIA yêu cầu reboot, installer dừng trước bước build. Khởi
động lại Windows rồi chạy lại đúng lệnh trên; các bước đã xong được nhận diện và bỏ qua,
không cài trùng.

Không sao chép `.env.windows`, `.venv-windows` hoặc binary `llama.cpp` đã build sang
máy khác. Installer pin UUID, compute capability và CUDA Toolkit của đúng GPU.

Các tùy chọn hữu ích:

```powershell
# Ép CPU mode; phù hợp MX250 hoặc máy chưa có GPU hỗ trợ
.\windows\install.ps1 -ComputeMode cpu -Profile minimal

# Chỉ dựng runtime, chưa tải model lớn
.\windows\install.ps1 -Profile minimal -SkipModels

# Chạy toàn bộ pytest sau khi cài
.\windows\install.ps1 -Profile auto -FullTest

# Giới hạn số job compile song song
.\windows\install.ps1 -Profile auto -BuildJobs 2

# Không tự cài prerequisite và không tự khởi động (chế độ quản trị nâng cao)
.\windows\install.ps1 -Profile auto -SkipPrerequisites -SkipStart

# Dùng bootstrap nhưng không tự mở trình duyệt
.\windows\bootstrap.ps1 -NoOpenDashboard
```

CPU mode đặt faster-whisper ở `int8`, llama.cpp ở `--n-gpu-layers 0` và chạy
TIGER-DnR bằng PyTorch CPU với chunk nhỏ hơn. Đây là đường tương thích để chạy được,
không phải cấu hình hiệu năng; nên thử video ngắn trước.

## Nâng cấp từ CPU lên RTX

Sau khi lắp RTX được hỗ trợ và cài/reboot driver nếu Windows yêu cầu, chạy lại
bootstrap với GPU mode. Installer giữ model/data, tạo binary llama.cpp CUDA riêng và
tự thay wheel PyTorch CPU bằng `cu128`:

```powershell
.\windows\bootstrap.ps1 -ComputeMode gpu -Profile auto
```

Không cần xóa `.venv-windows`, `.env.windows` hoặc thư mục model. Nếu card mới vẫn
không đạt ma trận release, lệnh dừng trước khi cài CUDA và hướng dẫn quay lại
`-ComputeMode cpu`.

Muốn đặt model, job và output ở ổ khác, bỏ dấu `#` và sửa `DUB_NATIVE_ROOT` trong
`.env.windows` trước khi chạy installer lần đầu.

## Chạy và dừng

```powershell
.\windows\stack.ps1 start
.\windows\stack.ps1 status
.\windows\stack.ps1 logs -Lines 200
.\windows\stack.ps1 restart
.\windows\stack.ps1 stop
```

Mở [http://127.0.0.1:8080/](http://127.0.0.1:8080/) rồi chọn upload file cục bộ.
Stack chỉ bind loopback và quản lý hai tiến trình `api` + `worker`. PID được đối chiếu
với đường dẫn Python và thời điểm khởi động trước khi dừng; `taskkill /T` dọn cả cây
tiến trình con như FFmpeg và `llama-server.exe`.

Có thể dùng CLI qua wrapper để khỏi activate virtual environment:

```powershell
.\windows\dub.ps1 doctor
.\windows\dub.ps1 models list
.\windows\dub.ps1 jobs list
```

## Khắc phục sự cố

- Installer yêu cầu reboot: khởi động lại Windows rồi chạy lại chính lệnh bootstrap;
  cache và prerequisite đã cài sẽ được dùng lại.
- MX250 hoặc GPU dưới 6 GiB: dùng `-ComputeMode cpu`; không cố cài CUDA 12.8 cho card
  đó. Chạy video ngắn để ước lượng thời gian trước khi xử lý phim dài.
- WinGet lỗi: chạy `winget --info`; installer dùng quy trình
  `Repair-WinGetPackageManager -Force -Latest` chính thức của Microsoft khi thiếu WinGet.
- Không tìm thấy Visual Studio sau auto-install: xem log `dd_*` mới nhất trong `%TEMP%`;
  workload bắt buộc là `Microsoft.VisualStudio.Workload.VCTools`.
- RTX 50 báo thiếu `sm_120`: gỡ CUDA cũ khỏi đầu `PATH`, cài CUDA 12.8 rồi xác minh
  `nvcc --list-gpu-arch | Select-String compute_120`.
- PyTorch báo `no kernel image`: chạy
  `.\.venv-windows\Scripts\python.exe -c "import torch; print(torch.__version__, torch.version.cuda, torch.cuda.get_arch_list())"`.
  Installer yêu cầu wheel `cu128` và từ chối tiếp tục nếu kiến trúc card không có trong
  danh sách kernel của wheel.
- CTranslate2 báo thiếu `cudnn64_9.dll`: cài cuDNN 9 cho CUDA 12.x, bảo đảm thư mục
  chứa DLL nằm trong `PATH`, rồi chạy lại installer. Runtime cũng thêm thư mục
  `torch\lib` của wheel PyTorch vào đường tìm DLL.
- Dashboard không mở: chạy `.\windows\stack.ps1 status`, sau đó xem
  `.\windows\stack.ps1 logs -Lines 200`.
- Cần chạy trực tiếp để quan sát lỗi: dừng stack rồi chạy
  `.\windows\stack.ps1 foreground`; nhấn `Ctrl+C` để kết thúc.

Model chỉ được tải trong bước quản trị cài đặt. Sau khi model đã cài và xác minh,
worker xử lý ASR, dịch, tách thoại, TTS và dựng video bằng tài nguyên local.

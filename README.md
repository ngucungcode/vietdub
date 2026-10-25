# VietDub

Tạo bản thuyết minh tiếng Việt hoàn toàn cục bộ trên NVIDIA GPU. Không cloud inference, telemetry hoặc tải model trong worker.

## Pipeline

`Upload MP4/MKV (+ SRT tùy chọn) → ASR/phụ đề → dịch tiếng Việt → tách thoại → TTS → timing → ducking/mix → MP4 + SRT + report`

Nguồn duy nhất là file người dùng upload. VietDub không tích hợp torrent, magnet, indexer hoặc download client.

- ASR: faster-whisper/CTranslate2.
- Dịch: Gemma 4 GGUF qua llama.cpp.
- Tách thoại: TIGER-DnR, giữ nhạc và hiệu ứng.
- TTS: VieNeu v2 hoặc Piper.
- Output: H.264/AAC, SRT tiếng Việt, timing report JSON.
- Checkpoint SQLite WAL, cancel/resume, artifact SHA-256.
- Glossary và context liên block giữ tên riêng/thuật ngữ nhất quán.

## Yêu cầu

### Ubuntu production

- Ubuntu 22.04 hoặc 24.04 x86_64.
- Python 3.11, 3.12 hoặc 3.13. Installer tự chọn interpreter có PyTorch CUDA hoạt động.
- NVIDIA GPU `sm_70`, `sm_75`, `sm_80`, `sm_86`, `sm_89`, `sm_90`; `sm_120` experimental.
- RAM tối thiểu 16 GiB.
- CUDA 12.6 hoặc 12.8 và driver tương thích.
- `nvidia-smi` và `nvcc` hoạt động.

### Windows native

Windows 10/11 x64 hỗ trợ dashboard/API/worker native, không cần WSL hoặc Docker. Installer tự kiểm Python, Git, CMake, Ninja, FFmpeg, Visual Studio Build Tools, NVIDIA driver và CUDA.

## Cài đặt

### Cài nhanh một lệnh / một cú nhấp

Ubuntu (máy trống, chỉ cần `curl` + `sudo`):

```bash
curl -fsSL https://raw.githubusercontent.com/ngucungcode/vietdub/main/quick-install.sh | sudo bash
```

Windows: nhấp đúp `quick-install.bat` trong source (hoặc tải riêng file
đó rồi nhấp đúp). Chi tiết, tùy chọn và lưu ý an toàn: xem
`docs/one-command-install.md`.

### Ubuntu

```bash
git clone https://github.com/ngucungcode/vietdub.git
cd vietdub
sudo ./install.sh --profile auto --acceptance basic --yes
```

### Windows

```powershell
git clone https://github.com/ngucungcode/vietdub.git
cd vietdub
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\windows\bootstrap.ps1
```

Mở `http://127.0.0.1:8080/`.

## Docker

```bash
cp .env.example .env
docker compose up --build
```

Compose chỉ publish API qua `127.0.0.1`; worker không có network.

## Bảo mật

- Local mode: Host allowlist, DNS-rebinding protection và security headers.
- Remote mode: Bearer token tối thiểu 32 byte; token chỉ nằm ở server/reverse proxy.
- Admin API luôn yêu cầu loopback + `X-Dub-Admin-Request: 1`.
- Model pin revision + file/tree SHA-256; worker offline.
- Upload quota/reservation durable; completed-job retention và pinning.
- Python locks có hash; SBOM gồm dependency transitive.

Remote deployment phải dùng TLS reverse proxy. Xem `docs/dependency-security.md` và `docs/QUALITY.md`.

## Chất lượng và GPU acceptance

```bash
python scripts/quality-eval.py \
  --corpus /secure/quality-corpus.json \
  --output /secure/quality-report.json

sudo scripts/native-phase4-acceptance.sh
```

Không công bố GPU/model là production nếu chưa chạy acceptance trên phần cứng thật. Corpus đánh giá phải là nội dung được phép sử dụng và không được commit vào repository public.

## CLI

```bash
dub health
dub models list
dub models install-profile auto --yes
dub jobs list
dub upload movie.mkv --i-have-rights --watch
```

## Cấu hình chính

| Biến | Mặc định |
|---|---|
| `DUB_API_ACCESS_POLICY` | `remote` fail-closed; installer local đặt `local` |
| `DUB_API_ALLOWED_HOSTS` | `127.0.0.1,localhost,::1` |
| `DUB_API_TOKEN_FILE` | `/run/secrets/api_token` |
| `DUB_UPLOAD_MEDIA_MAX_BYTES` | 16 GiB |
| `DUB_INCOMING_QUOTA_BYTES` | 100 GiB |
| `DUB_COMPLETED_RETENTION_DAYS` | 30 |
| `DUB_EVENT_RETENTION_DAYS` | 14 |

## Pháp lý

Chỉ xử lý nội dung, phụ đề và giọng nói bạn sở hữu hoặc được cấp quyền. VietDub không vượt DRM, không xác định quyền sử dụng thay người vận hành.

GPL-3.0-or-later. Xem `LICENSE` và `THIRD_PARTY_NOTICES.md`.

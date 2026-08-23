# Khớp khẩu hình offline

VietDub hỗ trợ Phase 5 tùy chọn sau khi đã tạo audio thuyết minh:

- `off` — không sửa frame; mặc định.
- `draft` — MuseTalk 1.5, tối thiểu 4 GiB VRAM theo upstream.
- `final` — LatentSync 1.6 512 px, tối thiểu 18 GiB VRAM theo upstream.

Worker không tải model hoặc source ở runtime. Hai backend phải được cài trước, pin revision, và hoàn toàn nằm trên máy.

## Backend pin

| Backend | Source revision | Model revision | License cần kiểm |
|---|---|---|---|
| MuseTalk | `0a89dec45a0192b824e3cf4daf96c239440c5ed8` | `3ef28bc5cff08c90ad8178a25f1b570cd800170f` | code MIT; checkpoint HF khai báo `creativeml-openrail-m`; kiểm license từng model phụ |
| LatentSync 1.6 | `a229c3948406bc2cf6eaf4873e662e70c6a04746` | `c42c7e6c8e9c213626389fa7d9a3c444b8536353` | code Apache-2.0; checkpoint HF khai báo `openrail++` |

VietDub khóa `latentsync_unet.pt` bằng SHA-256
`0a478e89eb660f82da4c35dbdde8a5adfb27f99d1b4e50edd03729e1e98316d3` và từ chối file khác.

Không dùng test data của upstream cho sản phẩm thương mại.

## Layout mặc định

```text
/opt/lip-sync/
├── LatentSync/
│   ├── configs/unet/stage2_512.yaml
│   ├── configs/scheduler_config.json
│   ├── checkpoints/whisper/tiny.pt
│   └── latentsync/utils/mask.png
├── latentsync-venv/bin/python
├── MuseTalk/
│   ├── models/musetalkV15/{musetalk.json,unet.pth}
│   ├── models/sd-vae/...
│   ├── models/whisper/...
│   ├── models/dwpose/dw-ll_ucoco_384.pth
│   ├── models/syncnet/latentsync_syncnet.pt
│   └── models/face-parse-bisent/{79999_iter.pth,resnet18-5c106cde.pth}
└── musetalk-venv/bin/python

/models/lip-sync/latentsync-1.6/latentsync_unet.pt
```

Có thể đổi toàn bộ đường dẫn bằng biến `DUB_LATENTSYNC_*`, `DUB_MUSETALK_*`.

## Face-safety preflight

Trước khi load model nặng, VietDub lấy tối đa 16 mẫu chỉ tại các cửa sổ WAV có lời nói và chạy OpenCV trong môi trường backend:

- Không có mặt tại bất kỳ mẫu đang nói: giữ nguyên toàn bộ frame/video, audio Việt vẫn phát.
- Có hơn một mặt tại mẫu đang nói: coi là mơ hồ, không sửa mặt nào.
- Mặt duy nhất nhỏ hơn 1,5% diện tích frame: không sửa để tránh artifact.
- Chỉ khi mọi mẫu đang nói có đúng một mặt đủ lớn mới chạy MuseTalk/LatentSync.
- OpenCV và Haar frontal-face cascade phải có trong từng môi trường backend; thiếu detector thì fail-safe giữ nguyên hình.
- Kết quả cache tại `jobs/<id>/lip-sync/face-preflight.json`, khóa bằng SHA-256 video + audio + phiên bản gate.

Gate hiện bảo thủ ở cấp job, không đoán active speaker. Vì vậy phim có cảnh người nói ngoài khung hình sẽ giữ nguyên hình cho cả Phase 5 thay vì sửa nhầm diễn viên khác. Audio thuyết minh, SRT và timing không đổi. Shot-level active-speaker routing là bước nâng cấp riêng sau khi có corpus nhiều nhân vật và GPU nghiệm thu thật.

## Ràng buộc vận hành

- Backend chạy bằng argv, không qua shell.
- Worker ép `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, `DIFFUSERS_OFFLINE=1`.
- Video sinh hình bị bỏ audio; VietDub remux lại đúng AAC thuyết minh đã verify.
- Publish dùng file tạm + atomic replace; input giữ nguyên nếu backend lỗi trước publish.
- Backend có thể báo rõ `DUB_LIP_SYNC_NO_FACE` với exit code `3`; VietDub khi đó giữ video gốc và ghi fallback.
- Job `final` bị từ chối trước inference nếu GPU dưới 18 GiB; `draft` dưới 4 GiB.
- Checkpoint lưu mode, backend, SHA-256 đầu vào/đầu ra để resume không tái dùng output sai cấu hình.

## Nghiệm thu bắt buộc

Máy phát triển hiện tại MX250 2 GiB không chạy được hai backend. Trước khi công bố GPU support phải chạy video được cấp quyền trên card đích và lưu:

- peak VRAM, thời gian/frame và tỷ lệ fallback;
- sync score/LSE;
- identity similarity và temporal flicker;
- kiểm tra người nói đúng trong cảnh nhiều mặt;
- kiểm tra môi, răng, ria mép, profile và cảnh che miệng;
- SHA-256 source/model/output.

Không ghi PASS khi chỉ chạy fake backend hoặc unit test.

Nguồn chính thức:

- <https://github.com/TMElyralab/MuseTalk>
- <https://github.com/bytedance/LatentSync>
- <https://huggingface.co/TMElyralab/MuseTalk>
- <https://huggingface.co/ByteDance/LatentSync-1.6>

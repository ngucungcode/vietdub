# Cấu hình chuẩn cho máy 6 GB VRAM

> Card 6 GB "danh nghĩa" thường chỉ còn ~5.3-5.7 GB khả dụng sau
> driver + desktop. Mọi con số dưới đây chọn theo mức khả dụng đó,
> không theo mức danh nghĩa. Xem thêm `docs/gpu-6gb-separation-tts.md`
> để hiểu vì sao từng núm vặn lại là con số này.

## Áp dụng trong 1 nút bấm (khuyến nghị)

Trên dashboard, trong form tạo job → "Model và giọng nói nâng cao" →
**“Áp dụng cấu hình chuẩn 6GB VRAM”**. Nút này chỉ set cấu hình cho
job đó — nó **không tự tải model**. Tải model là nút riêng ngay bên
cạnh (xem mục dưới), vì model nặng hàng GB và không nên tự tải ngầm
khi bạn chỉ đang chọn cấu hình.

## Tải model còn thiếu từ dashboard

- **“Tải model còn thiếu cho preset 6GB”** (cạnh nút áp preset): tải
  nền trên server đúng 4 model của preset — ASR small, TIGER,
  Neucodec, VieNeu v2 — theo đúng thứ tự đó. Model nào đã cài và xác
  minh rồi thì bỏ qua, không tải lại.
- **“Tải model”** ở từng dòng trong mục Kho model: tải lẻ 1 model.
- Tiến trình: `POST /v1/models/install-6gb` hoặc
  `POST /v1/models/{model_id}/install` → 202, trạng thái xem ở
  `GET /v1/models/install-status`; dashboard tự poll mỗi 5 giây (tối
  đa 30 phút) rồi làm mới danh mục. Tải xong model hiện “SẴN SÀNG”.
- Nếu 1 model lỗi giữa chừng, job tải dừng ở đó (model đã xong vẫn
  giữ nguyên); bấm tải lại để tiếp tục từ model lỗi, vì model đã
  xong được bỏ qua.
- Tải khi đang rảnh, đừng tải đúng lúc job đang chạy stage dùng model
  đó. Restart server giữa chừng tải cũng an toàn: tải lại sẽ bỏ qua
  model đã xong.
- Cách cũ vẫn dùng được: từ thư mục source trên Windows,
  `.\windows\dub.ps1 models install tts-vieneu-v2` (và
  `tts-neucodec-onnx-int8`). Lưu ý: lệnh CLI `models install-profile
  balanced` **không** dùng được cho card 6GB — nó đòi tối thiểu 8192
  MiB VRAM và chặn ở CPU mode; tải lẻ từng model (hoặc nút dashboard)
  thì không bị 2 rào đó.

Bảng cấu hình preset set cho job đó:

| Bước | Giá trị preset | VRAM khi chạy riêng | Vì sao |
|---|---|---|---|
| ASR | `asr-faster-whisper-small` | 2048 MiB | `large-v3-turbo` (6144 MiB) sát nút trên 6 GB; small đủ cho phim thoại rõ |
| Dịch | Backend `api` (mặc định preset: DeepSeek) | 0 MiB | Giải phóng toàn bộ VRAM cho các bước GPU; đổi nhà cung cấp ở ô bên dưới được |
| Tách thoại | `separation-tiger-dnr`, chunk 30 s, context 2 s, batch 1 | ~6144 MiB định mức, nhưng chunk nhỏ nên activation nhỏ hơn nhiều so với chunk 120 s mặc định | Chunk là núm quyết định OOM của TIGER trên 6 GB |
| TTS | `tts-vieneu-v2` | 4096 MiB | Chuyên tiếng Việt, vừa 6 GB với batch 1 câu/block |
| QC | Bật (mặc định dashboard) | ASR small, chạy riêng sau export | Không chạy cùng lúc với stage khác |
| Duyệt cuối | Bật (mặc định dashboard) | 0 | Xem lại trước khi xuất file |

Quy tắc vàng không đổi: **không chạy 2 model GPU cùng lúc**.
Pipeline đã tách stage và đóng model sau mỗi stage — preset chỉ làm
cho từng stage vừa 6 GB, không phá quy tắc đó.

Chunk/context/batch tách thoại là trường **theo từng job**
(`separation_chunk_seconds`... trong job spec), nên preset không làm
ảnh hưởng job khác hay cấu hình máy chủ. Phase 4 đọc các trường này
lúc chạy và quay về mặc định máy chủ cho job không set chúng.

## Áp dụng cho cả máy chủ (không bắt buộc nếu đã dùng nút preset)

File `config/vietdub-6gb.env` chứa đúng các giá trị trên dưới dạng
biến `DUB_*` (ASR, separation, TTS, compute mode). Copy thành `.env`
của stack rồi restart API + worker. File này không chứa key API.

## Nếu không có API để dịch

Preset giả định bạn có API (DeepSeek mặc định, đổi được, hoặc server
local LM Studio / Ollama không cần key). Nếu nhất quyết dịch local:

- Model duy nhất vừa 6 GB là `mt-gemma4-e2b-q4` (6144 MiB, vẫn sát
  nút) — chọn backend Cục bộ + model này, và chấp nhận nó phải chạy
  RIÊNG, không cùng lúc với stage GPU khác.
- `mt-gemma4-31b-q4` (20480 MiB) **không chạy được** trên 6 GB, đừng
  chọn.
- Nếu TIGER vẫn OOM ở chunk 30 s: xuống chunk 15-20 s, rồi mới tới
  fallback chạy TIGER trên CPU (rất chậm) hoặc Piper thay VieNeu
  (0 VRAM, chất thấp hơn).

## Khi nào preset này không đủ

- Phim nhạc to, thoại chồng hiệu ứng: TIGER chunk 30 s có thể cho
  mối nối nghe rõ hơn chunk lớn; nếu cần chất tối đa thì chấp nhận
  chạy chậm (chunk nhỏ hơn nữa) hoặc chuyển đoạn khó sang máy mạnh hơn.
- ASR small nghe kém hơn turbo trên giọng khó / tiếng ồn lớn; nếu
  transcript sai nhiều thì quay lại turbo cho riêng tập đó và chấp
  nhận rủi ro sát nút VRAM (đóng mọi app dùng GPU khác trước).
- Máy bạn đang ở CPU mode (không có NVIDIA driver) thì preset GPU
  này chưa chạy được — nó chỉ có tác dụng khi máy đã nhận GPU 6 GB.

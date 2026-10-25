# Cấu hình toàn bộ qua frontend

Dashboard (`web/app/page.tsx`) giờ là nơi duy nhất cần chạm vào để cấu hình
một job: không cần sửa file hay biến môi trường cho các lựa chọn dưới đây.
Mọi lựa chọn trong form tạo job được gửi kèm khi tạo upload session
(`POST /v1/uploads`) và chảy nguyên văn vào job spec.

## Panel “Cài đặt” (`#settings`)

Đặt mặc định cho các job tạo sau đó (form tạo job vẫn đổi được từng job):

- **Key API** — `GET /v1/settings/api-key` (đường dẫn cũ
  `/v1/settings/openai-key` vẫn dùng được, cùng một file) chỉ trả
  `{configured: boolean}`, `PUT` lưu key, `DELETE` xóa key. Key không
  bao giờ được trả lại hay hiển thị trên dashboard. Thiếu key thì nút
  “Dịch bằng API” báo lỗi `openai_api_key_missing` và trỏ về panel
  này — trừ job trỏ về server local trên máy này (không cần key).
- **Backend dịch mặc định** — `local` (Gemma trên máy), `openai`
  (api.openai.com) hoặc `api` (API tùy chỉnh, xem
  `docs/custom-api-translation.md`).
- **Model API mặc định** — để trống thì backend `openai` dùng
  `gpt-4o-mini`; backend `api` bắt buộc phải có model của nhà cung cấp.
- **API base URL / nhà cung cấp mặc định** — chỉ khi backend là `api`,
  kèm preset DeepSeek / OpenRouter / Groq / LM Studio / Ollama.

## Form “Tạo job mới”

Ngoài các trường đã có (video/SRT, ngôn ngữ nguồn, nhịp lời, model từng bước,
giọng, glossary), form còn có:

- `translation_backend` (`local` | `openai` | `api`),
  `translation_model` (id model API; chỉ dùng khi backend là `openai`
  hoặc `api`) và `translation_api_base_url` (chỉ backend `api`, bắt
  buộc). Khi chọn `openai`/`api`, ô chọn model dịch cục bộ bị khóa và
  backend tự đặt `spec.models.translation` thành `mt-openai-<model>`
  hoặc `mt-api-<model>`.
- Nút **“Áp dụng cấu hình chuẩn 6GB VRAM”** (xem
  `docs/profile-6gb.md`): set ASR small, TIGER chunk 30 s / context
  2 s / batch 1, VieNeu v2, backend `api` (mặc định DeepSeek, đổi
  được). Ba ô chunk/context/batch tách thoại cũng sửa tay được và
  chỉ áp cho job đó (Phase 4 quay về mặc định máy chủ cho job khác).
- `require_final_approval` — **mặc định BẬT**. Job sẽ dừng ở trạng thái
  `awaiting_approval` thay vì hoàn tất ngay.
- `speaker_voices` — textarea, mỗi dòng `NHÃN=voice_id` hoặc
  `NHÃN=đường dẫn file giọng mẫu` (đuôi `.wav`/`.mp3`/`.flac`/`.ogg` hoặc chứa
  `/` hay `\` thì hiểu là file mẫu, còn lại là voice id). Đặt giọng theo
  nhân vật thì form bắt xác nhận quyền dùng giọng, giống như giọng chung.
- `diarize_speakers`, `auto_speaker_voices` (chỉ backend Piper),
  `qc_enabled` (mặc định BẬT).

Đổi bất kỳ trường nào trong số này sau khi đã giữ một upload session tạm đều
làm fingerprint của phiên thay đổi, nên dashboard bắt xóa phiên tạm trước khi
tạo phiên mới — giống hành vi cũ với các trường trước đó.

## Dịch bằng API trong job đang chạy

Khi job ở trạng thái `ready_translation` và `spec.translation_backend` là
`openai` hoặc `api`, panel điều khiển job hiện nút **“Dịch bằng API”**:

- `POST /v1/jobs/{id}/translate-openai` hoặc `/translate-api` (cả hai
  đường dẫn đều nhận cả hai backend) → `202 {job_id, status: "translating", model, base_url}`.
- Trong lúc `translating`, panel hiện trạng thái chờ; dashboard vốn đã tự
  làm mới mỗi 3 giây (cộng SSE) nên job tự chuyển sang `ready_tts` rồi Phase 4
  chạy tiếp, không cần bấm thêm.
- Các lỗi `409`: `openai_api_key_missing` (trỏ về panel Cài đặt),
  `translation_not_ready`, `translation_backend_not_openai`,
  `translation_in_progress` — tất cả hiển thị nguyên văn message của API.

## Panel “Duyệt kết quả”

Chỉ hiện khi job đang ở `awaiting_approval`. Dữ liệu từ
`GET /v1/jobs/{id}/review` (cùng shape với studio view, thêm
`preview_video_url` và `preview_subtitle_url`):

- Video player (dùng `preview_video_url`, fallback
  `GET /v1/jobs/{id}/review/video`), link tải phụ đề duyệt (fallback
  `GET /v1/jobs/{id}/review/subtitle`).
- Dòng tổng kết QC và từng câu thoại kèm cờ QC (giống cờ trong Studio).
  Các câu ở đây chỉ đọc; muốn sửa thì bấm “Cần sửa” rồi sửa trong Studio.
- **“Duyệt và xuất file”** — `POST /v1/jobs/{id}/review/approve`, job thành
  `completed` và panel tải kết quả cũ (`/v1/jobs/{id}/artifacts/...`) xuất hiện.
- **“Cần sửa”** — `POST /v1/jobs/{id}/review/reject` kèm `note` tùy chọn, job
  quay về `READY_TTS` và Studio mở lại để sửa.

## Chưa kiểm chứng

- Build/embed dashboard đã chạy được trên máy này (có `node_modules`
  trong `web/` từ lần build trước); **typecheck TypeScript chưa chạy
  riêng** — build `vinext` là kiểm chứng chính cho đợt này.
- Các endpoint mới (`/v1/settings/api-key`, `/v1/jobs/{id}/translate-api`,
  `/v1/jobs/{id}/review*`) chưa được thử với server thật; cần chạy thử
  trên máy GPU: tạo job `api` (DeepSeek hoặc LM Studio local) + duyệt
  cuối, job `local` không duyệt cuối, job `api` local không key, và
  trường hợp job `api` ngoài thiếu key (phải 409).

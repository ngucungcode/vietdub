# Dịch bằng API tương thích OpenAI (không bắt buộc api.openai.com)

> Tài liệu này thay thế phần "chỉ OpenAI" của `docs/openai-translation.md`.
> OpenAI vẫn dùng được như cũ (backend `openai`, mặc định
> `gpt-4o-mini`); backend mới `api` chỉ khác ở chỗ bạn tự nhập base URL
> và model của nhà cung cấp khác. Cả hai đều chạy qua cùng một đoạn
> code (`OpenAITranslator` trong `src/dub_server/openai_translation.py`),
> vì chúng nói cùng một giao thức: OpenAI-compatible Chat Completions.

## Điều kiện duy nhất: server phải có `/v1/chat/completions`

Bất kỳ server nào trả về đúng shape `POST /v1/chat/completions` đều
dịch được, bất kể nó chạy ở đâu:

| Nhà cung cấp | API base URL điền vào form | Model ví dụ | Cần key? |
|---|---|---|---|
| OpenAI (backend `openai`, không cần nhập URL) | — (mặc định) | `gpt-4o-mini` | Có |
| DeepSeek | `https://api.deepseek.com` | `deepseek-chat` | Có |
| OpenRouter | `https://openrouter.ai/api/v1` | `openai/gpt-4o-mini`, `deepseek/deepseek-chat`, ... | Có |
| Groq | `https://api.groq.com/openai/v1` | model Groq đang serve, ví dụ họ `llama-3.3-70b-versatile` | Có |
| LM Studio (máy này) | `http://localhost:1234` | tên model bạn đã load trong LM Studio | Không |
| Ollama (máy này) | `http://localhost:11434/v1` | tag Ollama, ví dụ `qwen2.5:14b` | Không |
| vLLM / text-generation-webui (OpenAI server) | URL server của bạn | tên model server đang serve | Tùy server |

Dashboard có sẵn preset cho từng dòng trên (ô "Nhà cung cấp API");
chọn preset chỉ điền sẵn URL/model, bạn vẫn sửa tay được sau đó.

**Không hỗ trợ:** API native khác shape — Anthropic Messages API,
Gemini native API, Cohere, ... Muốn dùng các nhà đó thì phải có server
trung gian dịch sang shape OpenAI-compatible (ví dụ LiteLLM proxy)
rồi trỏ VietDub vào proxy đó.

## Chuẩn hóa URL: dán cả `/v1` cũng được

Người dùng hay dán đủ kiểu URL. Server tự chuẩn hóa bằng
`normalize_api_base_url()` rồi mới cộng `/v1/chat/completions`:

- `https://api.deepseek.com` → `https://api.deepseek.com`
- `https://api.deepseek.com/v1` → `https://api.deepseek.com`
- `https://openrouter.ai/api/v1` → `https://openrouter.ai/api`
- `http://localhost:1234/v1/chat/completions` → `http://localhost:1234`

URL có `user:password@`, query hay fragment bị từ chối (400
`translation_api_base_url_invalid`) thay vì được gửi đi đâu đó.
URL phải là http/https và có host.

## Key: một key chung, server local không cần key

- Key nhập ở dashboard (Cài đặt) lưu chung một file 0600 phía server
  cho cả backend `openai` và `api`: `GET/PUT/DELETE
  /v1/settings/api-key` (đường dẫn cũ `/v1/settings/openai-key` vẫn
  dùng được, cùng một file). Key không bao giờ được trả lại.
- Thứ tự đọc key của runner/API: file đã lưu → file
  `/run/secrets/openai_api_key` → biến môi trường `OPENAI_API_KEY` →
  `TRANSLATION_API_KEY`. Key không nằm trong job spec, log hay artifact.
- Server loopback (`localhost`, `127.x`, `::1`) thường bỏ qua header
  Authorization, nên job trỏ về loopback được dịch ngay cả khi chưa
  lưu key (API tự dùng key giả `"local"`). API ngoài loopback mà thiếu
  key thì vẫn 409 `openai_api_key_missing` như cũ.

## Luồng chạy (giống backend openai, chỉ khác URL/model)

1. Tạo job với `translation_backend: "api"`,
   `translation_api_base_url` (bắt buộc) và `translation_model`
   (bắt buộc — server của bạn không tự biết bạn muốn model nào, nên
   không có model mặc định cho backend này). Lúc finalize, backend
   chuẩn hóa `spec.models.translation` thành `mt-api-<model>` (ký tự
   `/`, `:`, `@` trong tên model kiểu OpenRouter/Ollama thành `-`).
2. Worker offline **bỏ qua** job này (nó chặn mạng, gọi API sẽ lỗi).
   Job nằm yên ở `READY_TRANSLATION`.
3. Bấm **“Dịch bằng API”** trong panel job →
   `POST /v1/jobs/{id}/translate-api` (đường dẫn cũ
   `/translate-openai` cũng nhận cả hai backend) → 202, API chạy
   `TranslationStage` với `OpenAITranslator(base_url=...)` trong
   background task. Xong, job sang `READY_TTS` và worker chạy tiếp
   Phase 4 như bình thường.
4. Script CLI vẫn dùng được, nhưng phải truyền đúng URL/model của job:
   `PYTHONPATH=src python3 scripts/translate-openai.py --base-url
   https://api.deepseek.com --model deepseek-chat --once`.
   Script không tự đọc base URL từ job spec — bấm nút trên dashboard
   thì không lo chuyện này vì nút đọc đúng spec của job đó.

## Cái gì được gửi đi, cái gì không

Giống backend OpenAI cũ, chỉ khác đích đến: text từng block dịch +
glossary + 1 block tiếng Việt trước đó (giữ tên riêng nhất quán) được
gửi lên nhà cung cấp bạn chọn. Không gửi tên file, audio hay toàn bộ
kịch bản trong 1 request. Dùng API ngoài nghĩa là bước dịch không còn
offline — ASR, tách thoại, timing và TTS vẫn local.

## Giới hạn đã biết

- Một request / block, `temperature=0`, retry 429/5xx có backoff,
  401/403/400 fail ngay — giống backend OpenAI cũ.
- Model của nhà cung cấp khác có thể dịch kém hơn hoặc khác style so
  với `gpt-4o-mini`; test 1 tập ngắn trước khi chạy cả phim, và bật
  duyệt cuối (`require_final_approval`, mặc định bật trên dashboard)
  để xem lại trước khi xuất file.
- Chưa test với key thật của từng nhà cung cấp (cần key của bạn và
  một job ở `READY_TRANSLATION`); phần normalize URL và request shape
  đã test bằng mock HTTP (xem "Kiểm tra đã làm" ở cuối file này).

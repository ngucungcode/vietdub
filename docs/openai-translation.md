# Dịch bằng OpenAI API (opt-in, thay cho Gemma local)

> OpenAI là một trường hợp riêng của backend API chung: muốn dùng nhà
> cung cấp khác (DeepSeek, OpenRouter, Groq, LM Studio, Ollama, ...)
> thì xem `docs/custom-api-translation.md` — cùng code, chỉ khác base
> URL và model. Tài liệu này giữ lại cho backend `openai` cũ, vẫn
> dùng được nguyên vẹn.

> Mặc định repo vẫn offline: dịch bằng Gemma 4 local qua llama.cpp.
> Tài liệu này chỉ dùng khi bạn **chủ động** chấp nhận gửi transcript
> (không gửi video/audio) ra OpenAI để đổi lấy chất lượng dịch cao hơn
> và giải phóng toàn bộ VRAM cho các bước GPU khác.

## Cái gì được đưa lên OpenAI, cái gì không

| Bước | Sau khi bật OpenAI |
|---|---|
| ASR (faster-whisper) | Vẫn local, không đổi |
| **Dịch** | **OpenAI Chat Completions, 1 request / block** |
| Tách thoại (TIGER-DnR) | Vẫn local, không đổi |
| Timing / ducking / mix | Vẫn local, không đổi |
| TTS (VieNeu / Piper) | Vẫn local, không đổi |

Chỉ text của từng block dịch + glossary + 1 block tiếng Việt trước đó
(làm context giữ tên riêng) được gửi đi. Không gửi tên file, không gửi
audio, không gửi toàn bộ kịch bản trong 1 request.

## Code đã thêm

- `src/dub_server/openai_translation.py` — `OpenAITranslator`
  implements đúng `Translator` protocol trong `translation.py`:
  `count_tokens`, `translate_batch`, `translate_batch_for_durations`,
  `configure_translation_context` (glossary + context trước), `close`.
- `src/dub_server/translation_stage.py` — thêm tham số
  `openai_translator: Translator | None = None` cho `TranslationStage`.
  Khi set tham số này, stage **bỏ qua model resolver / models.lock**
  cho bước dịch, ghi provenance là `model_id` bạn chọn
  (ví dụ `mt-openai-gpt-4o-mini`) + `prompt_template_id =
  "openai-translation-v1"` + tree hash tổng hợp từ model id.
  Block building, checkpoint từng block, retry khi block rỗng,
  và path rút gọn theo duration (NATURAL profile) giữ nguyên.

Prompt giữ nguyên ngữ nghĩa của backend llama local:
deterministic (`temperature=0, top_p=1, seed=0`), giữ tên/số/quan hệ,
áp glossary chính xác, chỉ dùng context trước để giữ nhất quán,
không bịa thêm fact, chỉ trả về text dịch, không label/markdown.
Khi có target duration thì yêu cầu văn nói ngắn gọn vừa slot đó.

## Cách nối vào (quan trọng: worker offline chặn mạng)

Worker chính (`worker.py`) cài `install_offline_network_guard()` và
chỉ cho phép loopback tới cổng llama-server. Vì vậy **không** bật
OpenAI bên trong worker offline đó — request sẽ bị chặn ngay.

Cách đúng: chạy bước dịch ở một tiến trình riêng, ngoài worker,
dùng chung `StateStore`. Đã có script sẵn, chạy được luôn:

```bash
# Trên máy có GPU, ngoài worker offline; key ở /run/secrets/openai_api_key
PYTHONPATH=src python3 scripts/translate-openai.py            # gpt-4o-mini, lặp tới khi hết job
PYTHONPATH=src python3 scripts/translate-openai.py --model gpt-4o --once
```

Bản mẫu code (tham khảo, logic tương đương script trên):

```python
import asyncio, threading
from pathlib import Path
from dub_server.config import Settings  # hoặc settings của bạn
from dub_server.state import StateStore
from dub_server.openai_translation import OpenAITranslator, read_openai_api_key
from dub_server.translation_stage import TranslationStage
from dub_server.worker import process_next_translation_job

settings = Settings()  # điền models_lock_path / models_dir / jobs_dir
store = StateStore(settings.database_path)
translator = OpenAITranslator(
    api_key=read_openai_api_key(),   # /run/secrets/openai_api_key, fallback OPENAI_API_KEY
    model="gpt-4o-mini",
    max_output_tokens=512,
)
stage = TranslationStage(
    models_lock_path=settings.models_lock_path,
    models_dir=settings.models_dir,
    jobs_dir=settings.jobs_dir,
    default_translation_model_id="mt-openai-gpt-4o-mini",
    store=store,
    openai_translator=translator,
)
asyncio.run(process_next_translation_job(stage, shutdown=threading.Event()))
```

Cách khác (không khuyến nghị): sửa `worker.py` để bỏ qua
`install_offline_network_guard()` khi bật backend OpenAI. Làm vậy là
mở egress cho toàn bộ worker, không chỉ bước dịch — chỉ dùng khi bạn
hiểu rõ và chấp nhận rủi ro đó.

## Key và chi phí

- Key: file `/run/secrets/openai_api_key` (khuyến nghị, giống pattern
  `opensubtitles_api_key` của repo) hoặc biến môi trường
  `OPENAI_API_KEY` khi chạy dev. Không commit key, không đưa vào job
  spec, log hay artifact.
- Model mặc định `gpt-4o-mini`. `gpt-4o` chỉ dùng cho pass chất lượng
  cuối trên tập ngắn — giá cao hơn khoảng 10-20 lần cho cùng số token.
- Vì stage gọi 1 request / block, một tập phim ~300-500 block sẽ là
  300-500 request. Ước tính trước bằng tổng token source x 2
  (source + output) rồi mới chạy. Retry: 429 và 5xx được retry với
  backoff 2s / 4s / 8s (tối đa 30s); 401/403/400 fail ngay, không retry.
- Token đếm trong adapter là ước lượng (`len/4`, tối thiểu 1) chỉ để
  chia block và ghi plan, **không** dùng để tính tiền OpenAI.

## Kiểm tra đã làm

- `python -m py_compile` cho cả 2 file: OK.
- Test với `httpx.MockTransport`: 429 lần đầu -> retry -> trả về bản
  dịch đã strip code fence; output rỗng -> raise `TranslationError`
  (stage sẽ fail job với code, không ghi bản dịch rỗng).
- Chưa test với key thật và chưa chạy end-to-end qua worker, vì cần
  key của bạn và một job ở trạng thái `READY_TRANSLATION`.

## Cấu hình qua frontend (không cần sửa file hay biến môi trường)

Upload mới nhận thêm 3 trường (đi thẳng vào job spec):

- `translation_backend`: `"local"` (mặc định, Gemma local) hoặc
  `"openai"`.
- `translation_model`: tên model OpenAI, ví dụ `gpt-4o-mini`
  (mặc định khi để trống). Lúc finalize, backend chuẩn hóa
  `spec["models"]["translation"]` thành `mt-openai-<model>` để
  stage, worker và runner đều đồng ý một model id.
- `require_final_approval`: xem vòng duyệt cuối trong
  `docs/studio.md`.

Luồng chạy của job `openai`:

1. Worker phiên dịch **bỏ qua** job này: `process_next_translation_job`
   nhận thêm lọc `translation_backend`, worker chính truyền `"local"`
   nên job `openai` nằm yên ở `READY_TRANSLATION` (nó mà nhận thì
   guard chặn mạng, dịch lỗi).
2. Frontend bấm `POST /v1/jobs/{job_id}/translate-openai` → 202, API
   (tiến trình không bị guard) chạy `TranslationStage` với
   `OpenAITranslator` trong background task. Job sang `READY_TTS`
   thì worker tự chạy tiếp Phase 4. Job `TRANSLATING` dở dang do
   API restart thì bấm lại để resume; job đang chạy rồi thì API
   trả 409 `translation_in_progress`.
3. Script CLI vẫn dùng được, và giờ cũng chỉ nhận job `openai`
   (`translation_backend="openai"`), không còn ôm cả job local như
   trước.

Key nhập qua UI, không qua biến môi trường:

- `GET /v1/settings/openai-key` → chỉ trả `{"configured": bool}`,
  không bao giờ trả key.
- `PUT /v1/settings/openai-key` với `{"key": "..."}` → lưu file
  0600. Vị trí: `DUB_OPENAI_API_KEY_FILE` nếu đặt, nếu không thì
  `<jobs_dir>/.openai_api_key` (server-side, không được serve).
- `DELETE /v1/settings/openai-key` → xóa file key.
- Thứ tự đọc key của runner/API: file đã lưu ở trên →
  `/run/secrets/openai_api_key` → biến môi trường `OPENAI_API_KEY`.
  Key không nằm trong job spec, log hay artifact (giữ nguyên luật
  cũ).

UI mới xem thêm `docs/frontend-config.md`.

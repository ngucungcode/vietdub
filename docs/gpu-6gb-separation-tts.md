# Chạy tách thoại mạnh + TTS Việt mạnh trên GPU 6 GB VRAM

Nguyên tắc vàng cho 6 GB: **không chạy 2 model GPU cùng lúc**.
Repo đã tách stage (ASR -> dịch -> tách thoại -> TTS) và mỗi stage
đóng model trước khi mở stage sau — giữ nguyên nguyên tắc này.
Thứ tự thực tế cho 1 tập phim trên 6 GB:

```
ASR (faster-whisper small/turbo, fp16) -> unload
  -> Dịch: OpenAI (0 VRAM) hoặc Gemma E2B Q4 (xem dưới)
  -> Tách thoại: 1 model, chunk nhỏ, batch=1 -> unload, empty_cache
  -> TTS: 1 model, batch=1, câu ngắn -> unload
```

Đừng cộng dồn VRAM các model trong `models.lock.json` lại với nhau:
con số đó là "mỗi model khi chạy riêng", không phải tổng chạy cùng lúc.

## 1. Tách thoại (dialogue / music / effects, DnR)

Mục tiêu ở đây khác tách vocal nhạc: cần giữ **thoại sạch** để ducking
và giữ **nhạc + hiệu ứng** làm nền. Repo hiện dùng TIGER-DnR theo
đúng hướng này (chỉ giữ music+effects, thoại được thay bằng TTS).

### Cách A — Giữ TIGER-DnR, nhưng ép vừa 6 GB (khuyến nghị đầu tiên)

Hiện tại `models.lock.json` ghi TIGER-DnR cần tối thiểu 6144 MiB,
chạy float32, và runtime trong `audio_separation.py` đã stream theo
chunk (mặc định `separation_chunk_seconds=120`, context 4s, batch 1).
Trên card 6 GB "danh nghĩa", VRAM khả dụng thực tế thường chỉ
~5.3-5.7 GB sau driver + desktop, nên 6144 MiB là **sát nút**.

Ép vừa bằng 3 núm vặn đã có sẵn, không cần đổi model:

```bash
# chunk nhỏ hơn -> activation nhỏ hơn, đổi lại chậm hơn và nhiều mối nối hơn
# context 0-2s thay vì 4s, batch giữ = 1
# (đặt trong settings của bạn: separation_chunk_seconds,
#  separation_context_seconds, separation_batch_size)
separation_chunk_seconds = 30   # thử 30 trước, nếu OOM xuống 15-20
separation_context_seconds = 2
separation_batch_size = 1
```

Nếu vẫn OOM: chạy TIGER trên CPU cho tập đó (chậm hơn nhiều lần
nhưng không OOM), hoặc chuyển sang Cách B. Đừng cố load cả 2 nhánh
effect + music cùng lúc với chunk 120s trên 6 GB.

### Cách B — Demucs / BS-RoFormer, fp16 + chunk overlap-add

Khi bạn chấp nhận tách **vocal** thay vì tách DnR 3 stem chuẩn
điện ảnh, 2 họ model này vừa 6 GB thoải mái hơn:

- **HTDemucs (`htdemucs_ft`, fp16)**: chạy `demucs --two-stems=vocals`
  hoặc 4 stems, `segment` nhỏ. Ưu: ổn định, dễ cài, tiếng vocal
  sạch cho phim thoại rõ. Nhược: tách theo nhạc, nhạc nền có lời
  hát dễ lọt vào stem vocal; hiệu ứng/nhạc nền không tách riêng
  thành 2 stem như DnR.
- **BS-RoFormer (vocal model, fp16)**: chất vocal thường cao hơn
  Demucs trên nhạc, tham số chỉ ~100-150 MB, activation khoảng
  2-4 GB với chunk ~8s ở 44.1 kHz — vừa 6 GB nếu chunk nhỏ và
  overlap-add (fade ở mối nối, repo đã làm kiểu này cho TIGER).
  Nhược: vẫn là tách vocal nhạc, không phải DnR; cần test trên
  đoạn phim có nhạc nền + thoại chồng nhau trước khi dùng đại trà.

Cách nối vào repo: viết thêm một `SeparationBackendRunner` mới
(giống `TigerDnrSubprocessRunner` trong `audio_separation.py`:
nhận `SeparationBackendRequest`, chỉ được ghi accompaniment WAV
vào `work_dir`, trả `BackendSeparationResult`) rồi đăng ký model id
mới trong `models.lock.json`. Đừng sửa trực tiếp
`CinematicAudioSeparator` — nó chỉ validate output, không biết
backend nào chạy.

### Cách C — Dùng Demucs làm "pre-clean" rồi TIGER làm pass 2 (chất cao nhất, chậm nhất)

1. Demucs fp16 tách vocal thô, nhanh, vừa VRAM.
2. Chỉ đưa phần "không phải vocal" + vocal thô vào TIGER với chunk
   nhỏ để tách lại music/effects sạch hơn.
3. Nếu OOM ở bước 2 thì fallback: giữ kết quả Demucs, không fail job.

Cách này chỉ nên dùng cho phim điện ảnh / đoạn khó (nhạc to,
thoại chồng hiệu ứng). Phim truyền hình thoại rõ thì Cách A hoặc B
là đủ, nhanh hơn nhiều.

### Bảng chọn nhanh — tách thoại

| Cách | Chất DnR | VRAM 6 GB | Tốc độ | Khi nào dùng |
|---|---|---|---|---|
| A. TIGER chunk 30s, ctx 2s, batch 1 | Cao nhất | Sát nút, cần test OOM | Trung bình | Mặc định, phim điện ảnh |
| B. Demucs / BS-RoFormer fp16 | Tốt (vocal) | Thoải mái (~3-4 GB) | Nhanh | Phim thoại rõ, cần chạy nhanh |
| C. Demucs + TIGER pass 2 | Cao nhất | Từng bước một, vẫn vừa | Chậm nhất | Đoạn khó, nhạc chồng thoại |
| CPU fallback | Như A | 0 VRAM | Rất chậm | Khi GPU OOM mà vẫn muốn giữ TIGER |

## 2. TTS tiếng Việt mạnh

### Cách 1 — VieNeu v2 (đang có sẵn trong repo, khuyến nghị)

Repo đã có `tts-vieneu-v2` (`pnnbao-ump/VieNeu-TTS-v2`, min VRAM
4096 MiB, fp16) qua `VieNeuNarrationSynthesizer` /
`PersistentVieNeuNarrationSynthesizer` trong `narration.py`.
Đây là lựa chọn vừa 6 GB nhất: 0.5B, huấn luyện riêng cho tiếng
Việt, clone giọng zero-shot từ 3-5s mẫu, và đã có sẵn path
normalize tiếng Việt (`normalize_vietnamese_for_tts`) trong repo.

Chạy trên 6 GB:

- Batch = 1 câu / block (stage TTS của repo đã làm theo block).
- Đừng giữ VieNeu persistent + model khác cùng lúc; đóng VieNeu
  trước khi sang bước mix (repo đã có `close` cho persistent adapter).
- Bản 0.3B nhẹ hơn nữa (~2x nhanh) nếu cần chạy song song với
  việc khác, đổi lại chất thấp hơn bản 0.5B một bậc.
- VieNeu v3 Turbo (48 kHz, ~10k giờ) là bản mới hơn cùng họ — nếu
  muốn nâng chất, test v3 trước khi thay v2, vì repo hiện pin v2.

### Cách 2 — Piper tiếng Việt (fallback 0 VRAM, luôn chạy được)

`tts-piper-vi-vais1000-medium` đã có trong lock, chạy CPU qua
`PiperNarrationSynthesizer`. Chất kém VieNeu, giọng robot hơn,
nhưng **không tốn VRAM** và không bao giờ OOM. Dùng làm fallback
khi VieNeu OOM, hoặc cho bản nháp nhanh trước khi render bản chính.

### Cách 3 — F5-TTS / XTTS-v2 fine-tune tiếng Việt (mạnh, cần test giọng)

- **F5-TTS**: khoảng 2-3 GB / worker ở fp16, vừa 6 GB với batch 1.
  Ưu: độ tự nhiên cao, clone giọng tốt. Nhược: cần bản fine-tune
  tiếng Việt tốt (bản gốc đa ngữ nhưng tiếng Việt không phải thế
  mạnh nhất); phải test phát âm dấu thanh, số, tên riêng trước.
- **XTTS-v2**: khoảng 3-4 GB / worker ở fp16, vừa 6 GB với batch 1.
  Ưu: clone giọng ổn định, cộng đồng dùng nhiều. Nhược: tiếng Việt
  cũng cần fine-tune / kiểm chứng, và tốc độ chậm hơn VieNeu trên
  cùng card.

Cả 2 đều nối vào repo theo cùng pattern `NarrationSynthesizer`
trong `narration.py`: nhận text đã normalize + speed, ghi WAV PCM
16-bit mono, trim silence theo chuẩn đã có. Đừng bỏ qua bước
normalize tiếng Việt của repo khi đổi backend — dấu thanh và viết
tắt là chỗ TTS nước ngoài hay sai nhất.

### Bảng chọn nhanh — TTS Việt

| Cách | Chất tiếng Việt | VRAM 6 GB | Khi nào dùng |
|---|---|---|---|
| VieNeu v2 0.5B (có sẵn) | Cao, chuyên Việt | ~4 GB, vừa | Mặc định |
| VieNeu 0.3B | Khá | Nhẹ hơn, nhanh hơn | Cần tốc độ / chạy cùng việc khác |
| Piper vi (có sẵn) | Trung bình | 0 (CPU) | Fallback, bản nháp |
| F5-TTS / XTTS-v2 fine-tune Việt | Cao nếu fine-tune tốt | 2-4 GB, batch 1 | Khi cần clone giọng riêng, đã test kỹ |

## 3. Cấu hình 6 GB gợi ý cho 1 job hoàn chỉnh

```
ASR:        faster-whisper-small (2048 MiB) hoặc large-v3-turbo (6144 MiB, sát nút)
Dịch:       OpenAI gpt-4o-mini (0 VRAM)  <- giải phóng nhiều nhất
             hoặc Gemma E2B Q4 local (6144 MiB, chạy riêng, không cùng lúc với bước khác)
Tách thoại: TIGER chunk 30s / ctx 2s / batch 1, fallback Demucs fp16 nếu OOM
TTS:        VieNeu v2, batch 1 câu, fallback Piper nếu OOM
Quy tắc:    sau mỗi stage: close model + torch.cuda.empty_cache()
            (repo đã làm cho TIGER runtime và llama close; áp dụng tương tự cho TTS)
```

Nếu vẫn OOM ở một stage, đừng tăng batch hay chunk — giảm chunk,
giảm context, chuyển stage đó sang CPU, hoặc chuyển dịch sang
OpenAI để lấy lại VRAM. Tăng VRAM ảo (swap) không cứu được OOM
của CUDA, chỉ làm chậm thêm.

# Studio lồng tiếng

Pipeline chạy một phát ăn ngay thì đủ cho bản nháp, nhưng lồng tiếng
thật cần vòng đạo diễn: nghe thử, đổi giọng, sửa câu, thu lại câu lỗi,
cân lại mix rồi mới chốt. Studio là vòng đó, xây trên checkpoint sẵn
có của Phase 4 thay vì chạy lại từ đầu.

## Khi nào sửa được

Studio chỉ mở với job ở hai trạng thái:

- `COMPLETED`: đã chạy xong toàn bộ. Mọi sửa đổi sẽ đưa job về hàng
  chờ Phase 4 (`READY_TTS`) để worker chạy lại.
- `READY_TTS`: đã dịch xong, chưa thuyết minh. Sửa đổi chỉ nằm trong
  spec và sẽ được áp dụng ở lần chạy Phase 4 đầu tiên.

Job đang chạy ở công đoạn khác thì API trả `studio_not_editable`:
đợi nó xong đã, tránh sửa checkpoint trong lúc worker đang ghi.

## Những gì sửa được

### Bảng cast — `PUT /v1/jobs/{job_id}/studio/cast`

```json
{"speakers": [
  {"speaker": "SPEAKER_00", "display_name": "Anna", "voice_id": "3"},
  {"speaker": "SPEAKER_01", "display_name": "Bob", "reference_path": null}
]}
```

- `display_name` chỉ để hiển thị, lưu ở spec `speaker_names`; giọng
  vẫn key theo nhãn gốc (`SPEAKER_00`), nên đổi tên không làm mất
  map giọng.
- Gửi cả `voice_id` và `reference_path` đều null thì xóa entry giọng
  của nhân vật đó (về giọng mặc định).
- Gán giọng mới yêu cầu job đã có `voice_rights_confirmed`, giống
  luật lúc upload.
- **Đổi giọng = thu lại toàn bộ.** Fingerprint checkpoint TTS bao
  phủ cả map giọng, nên đổi một nhân vật cũng làm mất hiệu lực mọi
  block cũ và Phase 4 tổng hợp lại từ đầu. Đó là hành vi có chủ ý
  của pipeline, không phải lỗi studio.

### Kịch bản — `PUT /v1/jobs/{job_id}/studio/script/{ordinal}`

Sửa câu chữ của một câu. Câu sửa lưu ở spec
`studio_line_overrides`, thắng cả bản dịch gốc lẫn timing rewrite cũ
của câu đó (rewrite cũ bị xóa lúc lưu, vì nó là bản rút gọn của câu
cũ, không được áp cho câu mới). Chỉ câu đó bị tổng hợp lại: block
cũ trượt kiểm tra hash text, timing fit lại câu đó, SRT và timeline
được build lại, rồi export chạy lại. Các câu khác giữ nguyên file
wav.

### Thu lại — `POST /v1/jobs/{job_id}/studio/retake`

```json
{"ordinals": [12, 13]}
```

Xóa block TTS của đúng các câu đó (giữ nguyên timing rewrite vì câu
chữ không đổi) rồi đưa job về hàng chờ. Dùng khi một câu bị rè, sai
ngữ điệu, hoặc sau khi đổi model TTS mà chỉ muốn thu lại vài câu.
Muốn thu lại toàn bộ thì đổi giọng (xem trên) hoặc xóa checkpoint
TTS — retake không có nút "tất cả" để tránh bấm nhầm một job dài.

### Mix — `PUT /v1/jobs/{job_id}/studio/mix`

```json
{"narration_lufs": -16.0, "accompaniment_lufs": -26.0, "ducking_ratio": 4.0}
```

Các trường hỗ trợ: `narration_lufs`, `accompaniment_lufs`,
`true_peak_db`, `ducking_threshold`, `ducking_ratio`,
`ducking_attack_ms`, `ducking_release_ms` (giới hạn giống
`MixSettings`). Đổi mix chỉ xóa checkpoint export, không động vào
TTS hay timeline: Phase 4 chạy lại sẽ dùng lại mọi block và timeline
cũ, chỉ mix + mux lại MP4 mới.

### Nghe thử từng câu — `GET /v1/jobs/{job_id}/studio/lines/{ordinal}/audio`

Trả file wav block TTS của câu đó (404/409 nếu câu chưa được tổng
hợp). UI studio dùng làm preview trước khi quyết định retake.

## AI tự nghe lại (QC)

Sau khi Phase 4 xuất xong MP4, pipeline tự chạy thêm một lượt ASR
trên timeline thuyết minh sạch (không nhạc, không hiệu ứng) và đối
chiếu từng câu với kịch bản đã thu. Kết quả nằm ở
`qc-report.json` trong thư mục job và hiện trong studio:

- Mỗi câu có `status`: `ok` (khớp 100%), `minor` (lệch ≤20% số từ),
  `review` (lệch ≤50%, nên nghe lại), `bad` (lệch trên 50%, gần như
  chắc chắn lỗi thật), `silent` (không nghe thấy gì trong khung giờ
  của câu). Kèm theo là câu AI nghe ra được (`heard`) và WER.
- Studio view trả `qc_summary`: tổng số câu, số câu khớp, số câu cần
  chú ý, WER trung bình.

QC là best-effort, không bao giờ làm fail job: thiếu model ASR, lỗi
nhận dạng, hay thiếu timeline thì chỉ ghi warning và bỏ qua. Tắt hẳn
bằng cờ `--no-qc` (CLI) hoặc spec `qc_enabled: false`. Vì report gắn
với hash timeline nên lần chạy lại chỉ đổi mix sẽ dùng lại report cũ
mà không chạy ASR nữa.

QC bắt được: câu bị TTS nuốt chữ, đọc sai thành chữ khác, đọc thừa,
hoặc câm. QC **không** bắt được: diễn xuất dở hay hay (nghe đúng chữ
vẫn có thể diễn đơ), sai thanh điệu mà ASR vẫn nhận ra cùng chữ, và
đọc nhầm giọng nhân vật (timeline đã trộn chung một track nên không
phân biệt được giọng ai — nhầm giọng vẫn phải soi bằng bản ghi voice
theo câu trong studio).

### Đọc toàn bộ studio — `GET /v1/jobs/{job_id}/studio`

Trả một object gồm `job_id`, `status`, `editable`, `cast` (nhân vật
theo thứ tự xuất hiện, kèm số câu và giọng), `script` (từng câu kèm
câu gốc, câu đã sửa, giọng, và `synthesized`), `mix` (giá trị hiệu
dụng sau khi gộp default), và `tts_backend` (thực chất là model id
TTS của job, để UI hiển thị đang dùng engine nào).

## Những gì chưa có

- **Nghe thử giọng mới (audition) trước khi gán:** cần tổng hợp ngoài
  job, chưa làm. Muốn thử giọng thì gán rồi retake một câu ngắn để
  nghe.
- **Sửa timing tay** (kéo thả câu trên timeline): timing vẫn do
  planner tự tính; sửa câu chữ là cách duy nhất can thiệp nhịp.
- **Đổi tên nhân vật trong SRT xuất ra:** SRT lấy text từ fitted
  block, còn tên nhân vật không nằm trong SRT — `display_name` chỉ
  phục vụ UI studio.

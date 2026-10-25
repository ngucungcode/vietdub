# Tách lời theo từng nhân vật (per-character voices)

VietDub tách lời thoại thành một stem dialogue chung (TIGER-DnR) rồi đọc
toàn bộ bằng một giọng. Tính năng này thêm một lớp trên đó: **mỗi câu
được gắn nhãn nhân vật, và stage TTS chọn giọng theo từng câu**. Nó
không tách sóng âm thanh theo người nói (xem giới hạn ở cuối).

## Nhãn nhân vật đến từ đâu

Nhãn được parse từ file phụ đề, không cần model mới:

- **SRT / WebVTT:** tiền tố `TÊN:` ở đầu dòng đầu tiên của cue.
  `ANNA: Xin chào` → speaker `ANNA`, câu đọc là `Xin chào`.
  Dòng bắt đầu bằng `- ` (hội thoại) vẫn nhận nhãn: `- BOB: ...`.
  Một dòng chỉ có `ANNA:` mà không có nội dung thì không tính là nhãn.
  Đồng hồ kiểu `12:30` không bao giờ bị nhận nhầm vì nhãn phải bắt đầu
  bằng chữ cái.
- **ASS:** trường `Name` trước, rồi `Style` nếu `Name` trống và style đó
  không phải style layout chung (`Default`, `Main`, ...). Tiền tố `TÊN:`
  trong nội dung vẫn được bóc ra khỏi câu đọc.

Nhãn được chuẩn hóa (gộp khoảng trắng, tối đa 120 ký tự) và đi xuyên
suốt pipeline:

```
TranscriptSegment.speaker → TranslationBlock.speaker → TranslationSegment.speaker
→ block TTS (checkpoint ghi thêm speaker / voice_id / reference_path)
```

Quy tắc quan trọng: **hai nhân vật khác nhau không bao giờ bị merge
chung một block dịch**. Một câu rất ngắn của nhân vật A kẹp giữa hai
câu của B sẽ đứng riêng thay vì làm fail cả job (trước đây block ngắn
bắt buộc phải merge với khối kề).

File phụ đề không có nhãn thì mọi câu có speaker `None` và chạy đúng
như trước, không đổi hành vi.

## Gán giọng cho từng nhân vật

Khai báo trong job spec, key `speaker_voices` (API `UploadCreateRequest`
có field này; CLI dùng `--speaker-voice` lặp lại được):

```json
"speaker_voices": {
  "Anna": {"voice_id": "3", "reference_path": null},
  "Bob": {"voice_id": null, "reference_path": "/workspace/voices/bob.wav"}
}
```

```bash
dub upload phim.mp4 --subtitle phim.srt --i-have-voice-rights \
  --speaker-voice "Anna=3" --speaker-voice "Bob=/workspace/voices/bob.wav"
```

- Key là nhãn nhân vật, khớp không phân biệt hoa thường (`ANNA` tìm
  được entry `Anna`). Nhân vật không có entry dùng giọng mặc định.
- Đổi `speaker_voices` sau khi job đã tổng hợp xong một phần sẽ làm
  checkpoint TTS cũ bị hủy (WAV cũ thuộc về giọng cũ) và tổng hợp lại
  từ đầu, tránh trộn giọng.
- API yêu cầu `--i-have-voice-rights` (field `voice_rights_confirmed`)
  khi có `speaker_voices`, giống như giọng tham chiếu chung.

## Giọng nào thực sự đổi được hôm nay

| Backend | voice_id số (Piper `--speaker`) | reference_path (VieNeu clone) |
| --- | --- | --- |
| Piper | Có, đổi thật theo từng câu | Không áp dụng |
| VieNeu | Không áp dụng | **Chưa**: entrypoint VieNeu hiện tại phục vụ một giọng cố định mỗi process và không nhận giọng theo từng request, nên câu của nhân vật vẫn đọc bằng giọng mặc định của entrypoint. `voice_id`/`reference_path` đã yêu cầu vẫn được ghi vào block checkpoint để lần nâng cấp entrypoint sau chỉ cần nối thêm một tham số, không phải sửa pipeline. |

Thực tế hôm nay: muốn mỗi nhân vật một giọng **chạy được ngay** thì
dùng Piper multi-speaker (đổi `speaker_id` theo câu). Muốn giọng
VieNeu clone theo nhân vật thì cần thêm tham số giọng vào entrypoint
`/opt/vieneu/vieneu-offline.py` (ngoài repo này) — đó là việc tiếp
theo, không phải việc trong repo.

## GPU 6 GB

Stage TTS giữ **tối đa một process tổng hợp mở cùng lúc**: pool đóng
process cũ trước khi mở process của giọng khác. Phim xen kẽ nhân vật
liên tục sẽ phải reload model theo từng lần đổi giọng — đó là chi phí
đánh đổi có chủ ý để không bao giờ ôm hai runtime GPU cùng lúc. Với
cast xen kẽ câu liên tục, Piper (khởi động process rẻ) hợp hơn VieNeu.

## Giới hạn và việc chưa làm

- **Không có diarization tự động.** Nhãn đến từ phụ đề; file không có
  nhãn thì không tự biết ai nói. Thêm diarization (ví dụ pyannote-audio
  chạy trên stem dialogue) là bước tiếp theo nếu cần, và nó chỉ gắn
  nhãn theo khoảng thời gian — đoạn hai người nói đè nhau vẫn không
  tách thành hai track riêng được.
- **Không tách sóng theo người nói.** Stem dialogue vẫn là tất cả lời
  nói gộp chung; tính năng này chỉ chia **giọng đọc lại** theo nhân
  vật, không chia audio gốc.
- Web UI chưa có ô nhập `speaker_voices`; hiện tại nhập qua API hoặc
  CLI.

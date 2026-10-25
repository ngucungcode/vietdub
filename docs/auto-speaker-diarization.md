# Tự động tách nhân vật và tự lồng giọng (diarization)

Luồng đầy đủ, không cần phụ đề có nhãn:

```
phim → ASR → diarization (pyannote, local) → gán SPEAKER_xx cho từng câu
     → dịch (không merge khác nhân vật) → TTS Piper tự gán giọng riêng
     → tách nền / mix → MP4
```

## 1. Cài đặt (máy GPU, làm một lần)

pyannote-audio và torch là dependency tùy chọn, không nằm trong
requirements mặc định của repo vì nặng và model diarization là gated:

```bash
pip install pyannote-audio torch --index-url https://download.pytorch.org/whl/cu121
```

Tải pipeline về thư mục cục bộ (cần chấp nhận điều kiện dùng model
`pyannote/speaker-diarization-3.1` trên Hugging Face và có token):

```bash
python - <<'EOF'
from pyannote.audio import Pipeline
pipe = Pipeline.from_pretrained(
    "pyannote/speaker-diarization-3.1",
    use_auth_token="TOKEN_CUA_BAN",
)
pipe.save_pretrained("/models/diarization/pyannote-3.1")
EOF
```

Sau đó trỏ server tới thư mục đó:

```bash
export DUB_DIARIZATION_PIPELINE_PATH=/models/diarization/pyannote-3.1
```

Không có token hoặc không muốn tải trước: vẫn chạy được bằng cách để
`DUB_DIARIZATION_PIPELINE_PATH` là hub id
`pyannote/speaker-diarization-3.1`, nhưng lần chạy đầu tiên sẽ tải từ
mạng — đi ngược triết lý offline của worker, nên chỉ dùng khi bạn chấp
nhận việc đó.

## 2. Chạy một job tự động

```bash
dub upload phim.mp4 --i-have-rights --i-have-voice-rights \
  --tts-model tts-piper \
  --diarize-speakers --auto-speaker-voices
```

- `--diarize-speakers` (spec `diarize_speakers`): bật diarization ở
  stage transcript. Nếu server chưa cấu hình pipeline thì job fail với
  mã `diarization_unconfigured` và thông báo rõ ràng, không chạy
  lặng lẽ không nhãn.
- `--auto-speaker-voices` (spec `auto_speaker_voices`): **chỉ có tác
  dụng với Piper**. Mỗi nhân vật chưa có entry trong `speaker_voices`
  được gán một `--speaker` id riêng: nhãn `SPEAKER_01` giữ số 1 nếu
  còn trống, nhãn khác lấy id trống thấp nhất theo thứ tự xuất hiện.
  Entry gán tay luôn thắng và không bị ghi đè.

Nhãn diarization là ẩn danh (`SPEAKER_00`, `SPEAKER_01`, ...). Muốn tên
thật (Anna, Bob) thì dùng phụ đề có nhãn (xem
`docs/per-character-voices.md`) hoặc đổi tên nhãn ở bước hậu kỳ — phần
map tên hiện vẫn là việc tay.

## 3. Nó hoạt động như thế nào, và giới hạn

- Diarization chạy trên file `source-audio-16k.wav` mà stage ASR đã
  decode sẵn, **trước** khi ASR load model và **tự giải phóng**
  pipeline ngay sau khi chạy xong, nên GPU 6 GB không bao giờ ôm hai
  model cùng lúc.
- Một câu được gán cho nhân vật chiếm ít nhất 50% thời lượng câu đó.
  Câu không ai chiếm đủ (cắt cảnh, nói đè) giữ `speaker=None` và đọc
  bằng giọng mặc định, thay vì đọc nhầm giọng nhân vật khác.
- Nhãn tay trong phụ đề luôn thắng diarization: diarization chỉ điền
  vào câu chưa có nhãn.
- Hai người nói đè nhau cùng lúc: diarization chỉ chọn người chiếm ưu
  thế trong câu đó, không tách thành hai track riêng. Muốn tách sóng
  thật thì cần separation theo speaker, ngoài phạm vi tính năng này.
- VieNeu: `--auto-speaker-voices` không có tác dụng vì entrypoint
  VieNeu hiện tại phục vụ một giọng cố định mỗi process (xem
  `docs/per-character-voices.md`). Muốn tự lồng giọng riêng từng nhân
  vật hôm nay thì dùng Piper.
- Chất lượng diarization phụ thuộc model và audio: phim ồn, nhạc nền
  lớn, hoặc nhân vật chỉ nói vài câu ngắn sẽ bị gán nhầm hoặc gộp
  chung. Với phim như vậy, phụ đề có nhãn vẫn là cách chắc chắn nhất.

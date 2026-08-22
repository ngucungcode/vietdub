# Nghiệm thu chất lượng

Không công bố chất lượng chỉ từ unit test hoặc fixture tổng hợp. Mỗi release model phải được đánh giá trên corpus mà người vận hành có quyền sử dụng.

## Corpus

Sao chép `config/quality-corpus.example.json`, thay mọi câu mẫu bằng dữ liệu được phép sử dụng và kết quả thật. Không commit media, transcript hoặc điểm nghe có bản quyền vào repository public.

Mỗi case gồm:

- `reference_vi`: bản dịch tham chiếu.
- `hypothesis_vi`: output pipeline.
- `glossary`: thuật ngữ bắt buộc.
- `human.adequacy`, `fluency`, `tts_naturalness`: 1–5, càng cao càng tốt.
- `human.dialogue_leak`: 1–5, 1 là không nghe thấy thoại gốc.

```bash
python scripts/quality-eval.py \
  --corpus C:/secure/quality-corpus.json \
  --output C:/secure/quality-report.json
```

Report đo WER tiếng Việt, tuân thủ glossary và trung bình điểm nghe. WER chỉ là regression signal, không thay COMET hoặc người chấm.

## GPU acceptance

Chạy acceptance trên từng GPU/driver/CUDA/profile được công bố:

```bash
sudo scripts/native-phase4-acceptance.sh
```

Tối thiểu ghi lại model revision/tree SHA-256, compute capability, driver, CUDA, RTF, peak VRAM/RSS, A/V sync, LUFS và checksum output. Không đánh dấu GPU `supported` nếu chưa chạy trên card thật. Máy không thuộc matrix phải fail-closed; không dùng kết quả CPU hoặc card khác làm bằng chứng.

## Cổng release

- Unit/integration/contract tests xanh trên Linux và Windows.
- Không có regression so với quality baseline đã duyệt.
- Không tăng dialogue leak.
- Không vượt VRAM profile.
- Artifact model và output khớp hash.
- Advisory dependency được sửa hoặc có VEX với đường code chứng minh không reachable.

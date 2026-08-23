# Workflow web upload-only

VietDub chỉ nhận nguồn bằng MP4/MKV người dùng tải lên. SRT là tùy chọn.
Không có tìm kiếm nguồn, torrent, magnet hoặc download client.

## Các bước

1. Chọn MP4/MKV.
2. Chọn SRT nếu có; nếu không, pipeline dùng ASR offline.
3. Chọn ngôn ngữ nguồn khi dùng SRT.
4. Chọn nhịp lời `natural` hoặc `strict`.
5. Chọn model/giọng nâng cao nếu cần.
6. Xác nhận quyền sử dụng và bắt đầu.
7. Theo dõi job; tải MP4, SRT và timing report khi hoàn tất.

## Upload protocol

```text
POST   /v1/uploads
PUT    /v1/uploads/{id}/media
PUT    /v1/uploads/{id}/subtitle   # tùy chọn
POST   /v1/uploads/{id}/finalize
DELETE /v1/uploads/{id}            # hủy/dọn phiên
```

Session, quota, SHA-256, path validation và atomic finalize được quản lý phía API.
Worker không có mạng và chỉ đọc artifact đã xác minh.

## Tích hợp phụ đề

OpenSubtitles có thể được cấu hình trong mục Phụ đề trực tuyến. Browser không đọc
lại secret. API quản trị chỉ chấp nhận loopback và header nội bộ từ proxy.

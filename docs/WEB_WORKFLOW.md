# Workflow web và tích hợp nguồn

## Luồng tải trực tiếp MP4/MKV + SRT

1. Mở `http://127.0.0.1:8080/` qua SSH tunnel, chọn tab **Tải file lên**.
2. Chọn video `.mp4` hoặc `.mkv`. Phụ đề `.srt` là tùy chọn; khi có SRT phải
   chọn một ngôn ngữ nguồn cụ thể thay vì **Tự động**.
   Đuôi file chỉ là bước kiểm tra đầu tiên. Luồng hình chính H.264/AVC được
   passthrough sang MP4; HEVC/H.265 SDR được mã hóa lại riêng phần hình sang H.264/AVC
   bằng CPU nên sẽ chậm hơn. HEVC HDR10, HLG và Dolby Vision bị từ chối để không xuất
   sai màu khi chưa có tone-map được kiểm thử. Server trả `unsupported_media` đối với
   VP8, FFV1, MPEG-4 Part 2 hoặc khi không có luồng hình chính hợp lệ; cover-art
   không được xem là luồng hình chính.
3. Chọn model/giọng và chế độ căn thời gian. **Nhịp tự nhiên** là mặc định: bản
   dịch được viết gọn theo thời lượng, giọng được giữ gần 1,0×, có thể mượn tối
   đa 0,8 giây khoảng lặng lân cận và không tăng quá 1,20×. **Khớp nghiêm ngặt**
   giữ timestamp phụ đề cũ và có thể phải thay đổi tốc độ nhiều hơn.
4. Xác nhận quyền rồi bắt đầu. Dashboard hiển thị riêng tiến độ gửi video, gửi
   phụ đề và bước xác minh/finalize.
5. Server ghi từng file vào `.part`, kiểm tra loại media và chỉ đổi tên atomic
   sau khi nhận đủ. Finalize tạo checkpoint acquisition/subtitle; từ thời điểm
   đó có thể đóng trang mà không mất job.
6. Không có SRT thì pipeline chạy ASR offline. Có SRT hợp lệ thì bỏ qua ASR và
   dùng chính timestamp/text trong file.

API dùng session ba bước để không giữ phim trong RAM:

```text
POST /v1/uploads
PUT  /v1/uploads/{id}/media
PUT  /v1/uploads/{id}/subtitle   # chỉ khi đã khai báo SRT
POST /v1/uploads/{id}/finalize
```

Có thể xem lại session bằng `GET /v1/uploads/{id}` hoặc hủy và dọn file dở bằng
`DELETE /v1/uploads/{id}`. Mặc định server nhận video tối đa 16 GiB, SRT tối đa
16 MiB và tự dọn định kỳ session chưa finalize theo TTL máy chủ, mặc định 7 ngày
(đồng thời quét ngay khi API khởi động). Mọi lỗi upload/finalize đều giữ nguyên
session để người dùng có thể sửa SRT hoặc thử lại; dashboard đọc lại checkpoint
và bỏ qua media/SRT đã có đúng
kích thước; CLI còn đối chiếu SHA-256 trước khi bỏ qua file. Khi request có
`Content-Length`, server từ chối trước khi đọc body nếu dung lượng đĩa không đủ.
Server kiểm lại checksum
trước khi finalize. Chỉ thao tác hủy/xóa rõ ràng hoặc TTL mới dọn session. Có thể đổi giới hạn bằng
`DUB_UPLOAD_MEDIA_MAX_BYTES`, `DUB_UPLOAD_SUBTITLE_MAX_BYTES` và
`DUB_UPLOAD_SESSION_TTL_SECONDS`.

## Luồng tìm nguồn qua Prowlarr

1. Mở `http://127.0.0.1:8080/` qua SSH tunnel và kiểm tra thẻ GPU/acquisition.
2. Tìm tên phim, có thể thêm năm; chọn đúng release hoặc dán `Release ID` từ kết
   quả tìm kiếm.
3. Chọn ngôn ngữ nguồn, chế độ phụ đề, chế độ căn thời gian và các model đã
   cài/verify. Nếu dùng giọng tham chiếu, chỉ chọn tệp giọng mà bạn có quyền sử
   dụng và xác nhận quyền giọng.
4. Xác nhận quyền đối với nội dung rồi tạo job.
5. Nếu job dừng ở `needs_subtitle_selection`, chọn một ứng viên hoặc chọn dùng
   ASR. Nếu dừng ở `needs_language`, chọn ngôn ngữ đúng để pipeline tiếp tục.
6. Theo dõi các stage acquisition → subtitle/ASR → translation → separation →
   TTS → timing → mix → export → verify. Dashboard hiển thị cả tiến độ tổng và
   tiến độ công đoạn hiện tại: dung lượng/tốc độ/ETA khi tải, số segment ASR,
   số block dịch/TTS/timing, tiến độ tách âm và thời lượng export đã xử lý. SSE
   cập nhật ngay khi job đổi trạng thái; polling định kỳ vẫn được giữ làm dự
   phòng nếu kết nối sự kiện bị ngắt.
7. Khi hoàn tất, tải MP4, phụ đề tiếng Việt SRT và timing report JSON. Có thể hủy
   job đang chạy hoặc tiếp tục job đã pause/lỗi có thể retry ngay trên web.

## Thêm indexer vào Prowlarr

Prowlarr sở hữu schema và credential riêng cho từng provider, nên dashboard
không tự dựng một biểu mẫu indexer chung. Tham khảo
[tài liệu/API Prowlarr](https://prowlarr.com/docs/api/); cách cấu hình:

1. Tạo tunnel cả API và Prowlarr:

   ```bash
   ssh -p SSH_PORT \
     -L 8080:127.0.0.1:8080 \
     -L 9696:127.0.0.1:9696 \
     root@GPU_HOST
   ```

2. Mở `http://127.0.0.1:9696`, vào **Indexers → Add Indexer**.
3. Chọn indexer mà tài khoản/khu vực của bạn cho phép, nhập credential, bấm
   **Test**, rồi **Save**.
4. Trở lại mục **Tích hợp** của dashboard, bấm **Kiểm thử tất cả**. Indexer hợp
   lệ sẽ được dùng trong ô tìm phim; không cần sao chép API key nội bộ của
   Prowlarr vào trình duyệt.

Chỉ cấu hình nguồn mà bạn được phép truy cập và chỉ tải nội dung bạn sở hữu hoặc
được cấp quyền. Dự án không cài sẵn tracker cụ thể.

## Kết nối OpenSubtitles API

OpenSubtitles yêu cầu các request sau đăng nhập tiếp tục dùng `base_url` trả về;
dashboard lưu route này sau khi giới hạn nó vào các host API chính thức. Xem
[tài liệu API OpenSubtitles](https://ai.opensubtitles.com/docs).

Backend chuẩn hóa thứ tự query tìm kiếm và chữ thường theo canonical URL của
OpenSubtitles. Mọi redirect còn lại bị từ chối thay vì tự động chuyển tiếp API
key/bearer token sang URL khác.

1. Đăng ký/đăng nhập OpenSubtitles và tạo API consumer key.
2. Trong mục **Tích hợp**, nhập API key, username và password rồi bấm lưu.
3. Backend gọi endpoint login chính thức, kiểm tra tài khoản trên đúng `base_url`
   được trả về rồi ghi atomic một bundle gồm `opensubtitles_api_key`,
   `opensubtitles_token` và `opensubtitles_base_url` với mode `0600`. Password chỉ
   tồn tại trong request đăng nhập và không được ghi xuống đĩa hoặc trả lại.
4. Chạy `dub stack restart`; sau đó mở lại dashboard để kiểm tra trạng thái.
5. Nếu token hết hạn, thực hiện đăng nhập lại. Nút xóa cấu hình xóa cả key, token
   và route API; nếu dọn file bị gián đoạn, dashboard hiện nút retry riêng.

Luồng ghi secret từ web dành cho deployment native, nơi `var/secrets` thuộc user
chạy API. Docker Compose mount `/run/secrets` chỉ đọc; ở profile đó hãy sửa file
secret trên host, đặt `DUB_OPENSUBTITLES_URL` trong `.env` đúng bằng `base_url`
trả về khi login, rồi chạy `docker compose up -d --force-recreate api worker`.

Quản trị tích hợp chỉ hoạt động từ loopback và header riêng của dashboard. Giao
diện production được FastAPI phục vụ cùng origin với API; không cần một web
server công khai khác.

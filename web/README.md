# Lồng Tiếng GPU Studio — Web

Dashboard tiếng Việt được tích hợp trực tiếp vào `thuyet-minh-offline-gpu`.
Bản production do FastAPI phục vụ cùng origin với `/v1/*`, vì vậy không cần
Node.js, reverse proxy hay cấu hình CORS trên máy GPU.

## Chức năng

- Kiểm tra trạng thái API, GPU, acquisition và catalog model.
- Tìm nguồn qua indexer đã được quản trị viên cấu hình.
- Chọn kết quả hoặc nhập `Release ID`, ngôn ngữ nguồn, chế độ phụ đề, model
  ASR/dịch/tách âm/TTS và giọng tham chiếu tùy chọn.
- Bắt buộc xác nhận quyền trước khi tạo job.
- Tự cập nhật danh sách job và tiến độ từng công đoạn mỗi 3 giây.
- Xử lý ngay trên web các trạng thái cần chọn ngôn ngữ hoặc chọn phụ đề.
- Hủy, tiếp tục, làm mới và tải MP4, SRT cùng timing report.
- Xem/trắc nghiệm kết nối Prowlarr và cấu hình OpenSubtitles mà không trả secret
  về trình duyệt.
- Hiển thị tốt trên desktop và điện thoại.

API hiện tại chưa có endpoint nhập magnet hoặc upload `.torrent` trực tiếp.
Web không giả lập tính năng này: cần bổ sung hợp đồng backend trước khi bật giao
diện tương ứng.

## Chạy bản đã tích hợp

Từ thư mục gốc của dự án:

```bash
dub serve
```

Mở `http://127.0.0.1:8080/`. API vẫn ở `http://127.0.0.1:8080/v1/*`.
Installer hiện có không cần chạy thêm dịch vụ web.

Để truy cập từ máy cá nhân tới GPU VM, giữ API bind loopback và dùng SSH tunnel:

```bash
ssh -L 8080:127.0.0.1:8080 -p SSH_PORT root@GPU_HOST
```

Sau đó mở `http://127.0.0.1:8080/` trên máy cá nhân.

Để nút “Mở Prowlarr” cũng hoạt động, thêm tunnel cổng quản trị:

```bash
ssh -p SSH_PORT \
  -L 8080:127.0.0.1:8080 \
  -L 9696:127.0.0.1:9696 \
  root@GPU_HOST
```

## Prowlarr và OpenSubtitles

Dashboard chỉ đọc danh sách indexer đã cấu hình và gọi kiểm thử tất cả indexer.
Việc thêm/sửa credential indexer vẫn diễn ra trong Prowlarr tại
`http://127.0.0.1:9696`: vào **Indexers → Add Indexer**, chọn dịch vụ mà bạn có
quyền sử dụng, nhập cấu hình của chính bạn rồi bấm **Test** và **Save**. Dự án
không đóng gói preset tracker/indexer và không nhận credential indexer tùy ý qua
API của dashboard.

Với OpenSubtitles, tạo API consumer key trong tài khoản OpenSubtitles rồi mở mục
**Tích hợp** trên dashboard. Nhập API key, tên đăng nhập và mật khẩu; backend dùng
chúng đúng một lần để đăng nhập, chỉ lưu API key, bearer token và `base_url` API
đã allowlist trong `var/secrets` với mode `0600`, và không lưu mật khẩu. Ba file
được commit/xóa như một bundle; lỗi cleanup có thể retry ngay trên dashboard.
Sau khi lưu hoặc xóa cấu hình, chạy `dub stack restart` để service acquisition
nạp lại secret. Token có thể hết hạn; khi đó đăng nhập lại từ cùng biểu mẫu.

Biểu mẫu ghi secret được thiết kế cho profile native trên GPU VM. Với Docker
Compose, `/run/secrets` thường là mount chỉ đọc; hãy cập nhật các file secret ở
host theo phần “Cấu hình ban đầu” của README, đặt `DUB_OPENSUBTITLES_URL` đúng
bằng `base_url` do login trả về, rồi force-recreate API thay vì dùng biểu mẫu.

Các endpoint quản trị này chỉ chấp nhận request loopback có header chủ ý của
dashboard. Vì vậy phải giữ API bind ở `127.0.0.1` và truy cập qua SSH tunnel;
không đưa dashboard này lên Internet nếu chưa có lớp xác thực và TLS riêng.

## Phát triển frontend

Yêu cầu Node.js 22.13 trở lên. Tạo `.env.local` nếu API không dùng địa chỉ mặc
định:

```dotenv
DUB_API_URL=http://127.0.0.1:8080
```

Chạy frontend ở cổng 3000 với route `/v1/*` chuyển tiếp về backend:

```bash
npm ci
npm run dev
```

Proxy phát triển chỉ chuyển tiếp API workflow thông thường và cố ý chặn
`/v1/admin/*`. Để thử cấu hình Prowlarr/OpenSubtitles, hãy chạy `npm run embed`,
khởi động `dub serve` và mở dashboard cùng origin ở cổng 8080 qua SSH tunnel.

## Cập nhật asset trong gói Python

Sau khi sửa frontend, chạy:

```bash
npm ci
npm run embed
```

Lệnh này build Vinext, render `index.html` và thay atomically thư mục
`src/dub_server/web_static`. Các file sinh ra được đóng gói trong wheel nhờ cấu
hình `package-data` của `pyproject.toml`; production không cần Node.js.

Không công khai cổng API, qBittorrent hoặc Prowlarr ra Internet. Dashboard này
không thay thế bước xác minh quyền sử dụng nội dung.

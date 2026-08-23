# VietDub dashboard

Dashboard local-first cho pipeline thuyết minh tiếng Việt.

## Luồng

1. Upload MP4/MKV từ thiết bị.
2. Upload SRT tùy chọn; không có SRT thì dùng ASR offline.
3. Chọn ngôn ngữ, profile timing, model và giọng.
4. Xác nhận quyền sử dụng.
5. Theo dõi job, tải MP4/SRT/timing report.

Không có tìm kiếm nguồn, torrent, magnet, Prowlarr hoặc qBittorrent.

## Chạy development

```bash
npm ci
npm run dev
```

Proxy `/v1/*` dùng `DUB_API_URL`, mặc định `http://127.0.0.1:8080`.
Bearer token được inject phía server bằng `DUB_API_TOKEN`; browser không lưu token.

## Kiểm tra

```bash
npm audit
npm run lint
npm test
npm run build
npm run embed
```

`npm run embed` render dashboard và copy asset vào `src/dub_server/web_static/`.
Mọi URL runtime `/_next/static/chunks/*` được rewrite thành `/assets/*` để FastAPI
phục vụ được sau reload.

## Bảo mật

- Media và secret ở lại máy xử lý.
- OpenSubtitles admin chỉ qua loopback + header nội bộ từ proxy.
- Không đọc lại API key/token/mật khẩu đã lưu.
- Chỉ MP4/MKV và SRT được chấp nhận ở upload boundary.

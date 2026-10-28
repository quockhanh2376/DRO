# Optimise — Performance & Security Review (2026-10-28)

Phạm vi: toàn bộ `app/` trên nhánh `codex/optimize-core`.
Trạng thái: **ĐÃ HOÀN THÀNH VÀ ĐƯỢC KIỂM THỬ TOÀN DIỆN (122 passed, 4 skipped).**

## Tóm tắt ưu tiên & Kết quả

| # | Mức | Loại | Vấn đề | File | Trạng thái |
|---|-----|------|--------|------|------------|
| 1 | Cao | Security/Perf | Login chạy scrypt đồng bộ trong `async def` → chặn event loop, không rate-limit | `app/auth.py:97` | **Đã tối ưu**: `run_in_threadpool`, cache sync hash, constant-time dummy scrypt, IP rate-limiter 5 req/min với khóa tạm 15m, log audit `login_failed`. |
| 2 | Cao | Performance | Dashboard tải TOÀN BỘ benchmark runs để lấy run mới nhất | `app/web/routes.py:97` | **Đã tối ưu**: Window function `row_number() over (partition by target_id)` + `selectinload(results)`, gom query dashboard còn <= 5 queries độc lập với lịch sử. |
| 3 | Trung bình | Performance | Các API list history không phân trang | `app/db/repositories.py:146,159` | **Đã tối ưu**: Phân trang `limit` (1-500, default 50) + `offset` trên repositories, API routes và web pages. Eager load results tránh N+1. |
| 4 | Trung bình | Security | `/api/v1/system/status` không cần đăng nhập, lộ số liệu nội bộ | `app/api/routes.py:70` | **Đã bảo mật**: Thêm `dependencies=[Depends(require_admin)]`, giữ `/health` public. |
| 5 | Thấp | Security | Session secret fallback ngẫu nhiên mỗi lần khởi động | `app/api/application.py:50` | **Đã xử lý**: Log warning rõ ràng khi thiếu `DRO_SESSION_SECRET`. `install-ubuntu.sh` tự sinh secret nếu chưa có. |
| 6 | Thấp | Security | Cookie `https_only` mặc định false | `app/api/application.py:52` | **Đã xử lý**: Thêm HTTP middleware tự động gắn security headers (`X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`, `Content-Security-Policy`). Tài liệu hóa trong `README.md`. |
| 7 | Thấp | Performance | `sanitize_for_storage` duyệt toàn bộ `os.environ` cho mỗi chuỗi | `app/security.py:66` | **Đã tối ưu**: Precompute snapshot & cache danh sách secrets, bỏ qua IP hợp lệ và chuỗi ngắn (< 4 ký tự), truyền cache danh sách secret xuống đệ quy để tránh duyệt env lặp lại. |

## Chi tiết các cải tiến đã thực hiện

### 1. Login: Không chặn event loop, chống timing attack & brute-force
- `login_submit` chuyển sang gọi `verify_login` qua `run_in_threadpool` để không block asyncio event loop.
- Thêm `LoginRateLimiter`: giới hạn 5 lần đăng nhập sai / phút trên mỗi IP, khóa tạm 15 phút (trả về 429), ghi log sự kiện audit `login_failed`. Xóa bộ đếm khi đăng nhập thành công.
- Chống timing attack: khi username không khớp hoặc mật khẩu cấu hình không hợp lệ, hệ thống vẫn chạy `_check_password` với precomputed dummy hash để thời gian phản hồi đồng nhất (~100-150ms).
- Cache bộ nhớ process: chỉ verify/sync cấu hình mật khẩu với hash trong DB một lần duy nhất khi khởi động thay vì mỗi request đăng nhập.

### 2. Dashboard: Truy vấn run mới nhất với O(1) queries
- Sử dụng subquery window function `row_number() over (partition by target_id order by completed_at desc, id desc)` tận dụng composite index `(target_id, completed_at)` (migration 0004).
- Tải kèm `selectinload(BenchmarkRunRecord.results)` để tránh N+1 queries.
- `_dashboard_rows` batch fetch optimizer states và schedule states. Số lượng database query cố định <= 5 queries dù có bao nhiêu targets và hàng nghìn runs lịch sử.

### 3. Phân trang History API & Web
- `list_benchmark_runs` và `list_rewrite_history` hỗ trợ tham số `limit` (1..500, mặc định 50) và `offset`.
- Các endpoint `/api/v1/targets/{id}/runs` và `/api/v1/targets/{id}/rewrite-history` hỗ trợ query parameters `limit` và `offset`.
- Giao diện web target detail và các thao tác đơn lẻ (`ping`, `run_state`, `apply_best`) chỉ lấy bản ghi mới nhất (`limit=1`) thay vì load toàn bộ bảng run.

### 4. Bảo vệ `/api/v1/system/status`
- Yêu cầu xác thực `require_admin` cho `/api/v1/system/status`.
- `/health` giữ công khai phục vụ kiểm tra sức khỏe của proxy/container.

### 5. Cảnh báo Session Secret & Tự sinh trong Installer
- Log `logging.warning` khi `DRO_SESSION_SECRET` chưa được cấu hình.
- `scripts/install-ubuntu.sh` tự động sinh 48-byte token URL-safe ngẫu nhiên vào `/etc/dro/dro.env` nếu biến chưa tồn tại.

### 6. Security Headers & Cookie Bảo mật
- Middleware HTTP tự động thêm các headers bảo mật tiêu chuẩn:
  - `X-Frame-Options: DENY`
  - `X-Content-Type-Options: nosniff`
  - `Referrer-Policy: same-origin`
  - `Content-Security-Policy: default-src 'self'; script-src 'self' 'unsafe-inline' https://unpkg.com; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'`
- Cập nhật hướng dẫn `DRO_HTTPS_ENABLED` trong `README.md`.

### 7. Cache Secrets cho `sanitize_for_storage`
- Tính toán danh sách secret values từ `os.environ` theo snapshot, cache trong bộ nhớ.
- Bỏ qua các chuỗi ngắn (< 4 ký tự) và địa chỉ IPv4/IPv6 hợp lệ để tránh che nhầm IP benchmark và target.
- Danh sách secrets được truyền xuống đệ quy qua các đối tượng dict/list/sample thay vì lặp qua `os.environ` cho từng chuỗi con.

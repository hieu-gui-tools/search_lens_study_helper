# 📷 Screenshot Lens

Ứng dụng chụp màn hình thông minh tích hợp **Google Lens** (qua Playwright Chromium headless) và **OCR** — viết bằng **PySide6**, quản lý bằng **uv**.

## ✨ Tính năng nổi bật

- **🖱 Chọn vùng linh hoạt**: Kéo thả chuột để chụp bất kỳ vùng nào trên màn hình.
- **⌨️ Phím tắt toàn cục (Hotkey)**: Sử dụng tổ hợp phím `Ctrl+Shift+S` để mở chế độ chụp nhanh mọi lúc mọi nơi.
- **📋 Tự động sao chép (Clipboard)**: Tự động sao chép ảnh chụp (PNG) vào bộ nhớ tạm để dễ dàng dán vào các ứng dụng khác.
- **💾 Lưu trữ tự động**: Tùy chọn lưu ảnh PNG kèm theo timestamp vào thư mục lưu trữ đã cấu hình.
- **🔍 Tích hợp Google Lens**: Upload ảnh tự động qua **Playwright Chromium headless** và trích xuất kết quả tìm kiếm thực tế từ Google Lens một cách trơn tru.
- **🖼 Khám phá ảnh tương tự (Visual Matches)**: Trả về danh sách các hình ảnh tương tự kèm theo liên kết nguồn trực tiếp.
- **🔤 Nhận diện văn bản (OCR)**: Hỗ trợ trích xuất văn bản từ hình ảnh (kết hợp sức mạnh của Lens và tuỳ chọn Tesseract cục bộ).
- **🔗 Tìm kiếm liên quan (Related Searches)**: Gợi ý các chủ đề và từ khóa liên quan từ Google.
- **📊 Giao diện Overlay hiện đại**: Tích hợp panel trượt tinh tế từ bên phải màn hình chia làm 4 tab hiển thị kết quả trực quan.
- **🔔 Hỗ trợ System Tray**: Ứng dụng chạy ngầm dưới khay hệ thống, double-click để kích hoạt hoặc mở menu chức năng.

## 🚀 Cài đặt & Hướng dẫn sử dụng

### 1. Yêu cầu hệ thống
- Python 3.9+
- Trình quản lý gói `uv`
- Môi trường Windows / macOS / Linux

### 2. Cài đặt

```bash
# Clone hoặc tải mã nguồn về máy
cd C:\Users\drhie\screenshot-lens

# Cài đặt trình duyệt Chromium cho Playwright (Chỉ cần chạy lần đầu)
uv run playwright install chromium

# (Tùy chọn) Cài đặt pytesseract để hỗ trợ OCR offline
uv add pytesseract
```

*(Lưu ý đối với OCR offline: Bạn cần tải và cài đặt [Tesseract](https://github.com/UB-Mannheim/tesseract/wiki) trên máy của mình trước)*

### 3. Chạy ứng dụng

Bạn có thể khởi động ứng dụng qua command line:
```bash
uv run python -m screenshot_lens
```

Hoặc trên Windows, bạn chỉ cần **double-click** vào file `run.bat` để chạy trực tiếp.

### 4. Sử dụng
- Nhấn `Ctrl + Shift + S` hoặc click vào biểu tượng ứng dụng ở khay hệ thống để bắt đầu chụp.
- Quét chọn vùng cần tìm kiếm trên màn hình.
- Chờ ứng dụng phân tích và thanh Panel kết quả sẽ tự động trượt ra ở góc phải màn hình để hiển thị thông tin trích xuất (Văn bản, Ảnh tương tự, Tìm kiếm liên quan).

## 🏗 Kiến trúc dự án

```text
screenshot-lens/
├── pyproject.toml              # Cấu hình project của uv
├── run.bat                     # Script khởi động nhanh cho Windows
├── LICENSE                     # MIT License
└── src/screenshot_lens/
    ├── app.py                  # Entry point khởi chạy
    ├── main_window.py          # GUI chính (PySide6)
    ├── capture.py              # Xử lý overlay chọn vùng + mss capture
    ├── lens_browser.py         # Playwright Chromium → Google Lens
    ├── search.py               # Quản lý luồng xử lý (QThread)
    └── overlay.py              # Panel hiển thị kết quả (4 tabs)
```

## 🔬 Cách hoạt động — Google Lens flow

1. **User chụp ảnh**
2. ➡️ Chuyển đổi PIL Image sang định dạng JPEG bytes
3. ➡️ Khởi tạo trình duyệt Playwright Chromium ở chế độ headless (chạy ngầm)
4. ➡️ Truy cập `lens.google.com`
5. ➡️ Upload ảnh tự động thông qua FileChooser API
6. ➡️ Đợi redirect tới URL kết quả `/search?p=...`
7. ➡️ Xử lý JavaScript extraction để lấy:
   - Văn bản OCR
   - Visual matches (Hình ảnh tương tự & Link)
   - Các tìm kiếm liên quan
8. ➡️ Đóng gói dữ liệu qua `LensResult` và gửi tín hiệu (`QThread signal`)
9. ➡️ Hiển thị lên giao diện qua Panel trượt `ResultOverlay`.

## 📜 Giấy phép (License)

Dự án này được phân phối dưới giấy phép **MIT**. Vui lòng xem file [LICENSE](file:///d:/ProjectRoot/PythonProject/screenshot-lens/LICENSE) để biết thêm thông tin chi tiết.

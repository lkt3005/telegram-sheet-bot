# Telegram Sheet Bot v2

Bot tự động đọc ảnh hàng về từ Telegram → ghi Google Sheet.
Bot hoạt động im lặng trong group, chỉ báo cáo riêng cho owner.

## Biến môi trường (Railway Variables)

| Tên | Giá trị |
|-----|---------|
| `TELEGRAM_TOKEN` | Token từ BotFather |
| `GEMINI_API_KEY` | API key từ Google AI Studio |
| `GOOGLE_SHEET_ID` | ID của Google Sheet |
| `GOOGLE_CREDENTIALS` | Toàn bộ nội dung file JSON service account |
| `OWNER_CHAT_ID` | Telegram ID của bạn (lấy từ @userinfobot) |

## Luồng hoạt động

### Mở phiên
Nhân viên gõ: `e nhận hàng 17.03` hoặc `e nhận hàng 17.03 aal`
→ Bot tạo tab mới trong sheet, thông báo riêng cho owner

### Wilson (ảnh + caption cùng lúc)
- Caption: `2s 3m 3L` hoặc `1m`
- Bot đọc mã SP, màu từ tag Wilson trong ảnh
- Bot đọc tracking, order number từ label ship trong ảnh
- Size + số lượng lấy từ caption

### Nike/Adidas (ảnh trước, text sau)
- Ảnh: bot đọc tracking từ label, lưu tạm
- Text: `fz6910-365 2L 1xL` hoặc `jz2207 2s`
- Bot ghép tracking + ghi sheet
- Nike: mã có dấu `-` (vd: fz6910-365)
- Adidas: mã không có dấu `-` (vd: jz2207)

### Đóng phiên
Nhân viên gõ: `hết`
→ Bot tổng kết số dòng đã ghi, thông báo riêng cho owner

## Cấu trúc Sheet
| STT | Hãng | Mã SP | Màu | Size | Số lượng | Tracking | Order Number |

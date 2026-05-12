import os
import json
import logging
import re
from datetime import datetime
import requests
import gspread
from google.oauth2.service_account import Credentials
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
GEMINI_API_KEY   = os.environ["GEMINI_API_KEY"]
GOOGLE_SHEET_ID  = os.environ["GOOGLE_SHEET_ID"]
GOOGLE_CREDS     = os.environ["GOOGLE_CREDENTIALS"]
OWNER_CHAT_ID    = int(os.environ["OWNER_CHAT_ID"])

# ── Trạng thái phiên nhận hàng (theo group chat_id) ──
# session[chat_id] = {"tab_name": str, "ws": worksheet, "stt": int, "pending_tracking": list}
sessions = {}

# ── Tracking tạm chờ text mã SP (Adidas/Nike) ──
# pending[chat_id] = [tracking1, tracking2, ...]
pending_tracking = {}


# ═══════════════════════════════════════════════
#  GOOGLE SHEET
# ═══════════════════════════════════════════════

def get_spreadsheet():
    creds_dict = json.loads(GOOGLE_CREDS)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds  = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(GOOGLE_SHEET_ID)

HEADERS = ["STT", "Hãng", "Mã SP", "Màu", "Size", "Số lượng", "Tracking", "Order Number"]

def create_tab(tab_name: str):
    """Tạo tab mới, thêm header, trả về worksheet."""
    ss = get_spreadsheet()
    try:
        ws = ss.add_worksheet(title=tab_name, rows=500, cols=len(HEADERS))
    except Exception:
        # Tab đã tồn tại
        ws = ss.worksheet(tab_name)
    ws.append_row(HEADERS)
    return ws

def append_rows(ws, rows: list):
    for row in rows:
        ws.append_row(row)


# ═══════════════════════════════════════════════
#  GEMINI
# ═══════════════════════════════════════════════

def call_gemini(image_bytes: bytes, prompt: str) -> str:
    import base64
    url = (
        "https://generativelanguage.googleapis.com/v1beta"
        f"/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
    )
    payload = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {"inline_data": {
                    "mime_type": "image/jpeg",
                    "data": base64.b64encode(image_bytes).decode()
                }}
            ]
        }]
    }
    r = requests.post(url, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()


PROMPT_WILSON = """Đây là ảnh gói hàng thể thao Wilson. Hãy đọc và trả về JSON:
{
  "tracking": "số tracking sau chữ TRACKING# hoặc TRAK# trên label vận chuyển, nếu không có thì null",
  "order_number": "số sau REF2: trên label, nếu không có thì null",
  "ma_sp": "mã sản phẩm trên tag Wilson (vd: WMD0274531)",
  "mau": "mã màu trên tag Wilson (vd: ZAB, NVD, RDB)",
  "size": "size trên tag Wilson (vd: S, M, L, XL)"
}
Chỉ trả về JSON, không giải thích."""

PROMPT_TRACKING_ONLY = """Đây là ảnh label vận chuyển (FedEx/UPS/USPS...).
Tìm TẤT CẢ số tracking trong ảnh — thường đứng sau chữ TRACKING#, TRAK#, hoặc TRACKING NUMBER.
Trả về JSON array: ["tracking1", "tracking2"]
Nếu không có thì trả về []
Chỉ trả về JSON, không giải thích."""


def gemini_wilson(image_bytes: bytes) -> dict:
    text = call_gemini(image_bytes, PROMPT_WILSON)
    text = text.replace("```json", "").replace("```", "").strip()
    return json.loads(text)

def gemini_tracking(image_bytes: bytes) -> list:
    text = call_gemini(image_bytes, PROMPT_TRACKING_ONLY)
    text = text.replace("```json", "").replace("```", "").strip()
    return json.loads(text)


# ═══════════════════════════════════════════════
#  PARSE CAPTION
# ═══════════════════════════════════════════════

def is_session_open(text: str) -> bool:
    return "e nhận hàng" in text.lower() or "nhận hàng" in text.lower()

def is_session_close(text: str) -> bool:
    return text.strip().lower() == "hết"

def extract_session_name(text: str) -> str:
    """'e nhận hàng 17.03 aal' → 'nhan hang 17.03 aal'"""
    text = text.lower().strip()
    # Lấy phần sau "nhận hàng"
    match = re.search(r"nh[aậ]n h[aà]ng\s*(.*)", text)
    suffix = match.group(1).strip() if match else ""
    return f"nhan hang {suffix}".strip()

def parse_nike_adidas_caption(text: str) -> list[dict]:
    """
    Parse các dòng như:
      'fz6910-365 2L 1xL'  → [{ma: fz6910-365, size: L, sl: 2}, {ma: fz6910-365, size: XL, sl: 1}]
      'jz2207 2s'          → [{ma: jz2207, size: S, sl: 2}]
      'kb4480 1xs'         → [{ma: kb4480, size: XS, sl: 1}]
      'ib0201-570 1L'      → [{ma: ib0201-570, size: L, sl: 1}]
    Một dòng có thể chứa nhiều mã SP (mỗi dòng 1 mã).
    """
    results = []
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]

    size_pattern = re.compile(r'(\d+)(xs|s|m|l|xl|xxl|3l|2xl)', re.IGNORECASE)
    # Mã SP: chữ+số có thể có dấu -
    ma_pattern = re.compile(r'^([a-zA-Z]{1,4}[\d][\w\-]*)', re.IGNORECASE)

    for line in lines:
        ma_match = ma_pattern.match(line)
        if not ma_match:
            continue
        ma_sp = ma_match.group(1)

        # Xác định hãng theo dấu -
        hang = "Nike" if "-" in ma_sp else "Adidas"

        # Tìm tất cả size+SL trong dòng
        sizes = size_pattern.findall(line)
        if not sizes:
            continue
        for sl, size in sizes:
            results.append({
                "hang": hang,
                "ma_sp": ma_sp,
                "size": size.upper(),
                "sl": int(sl),
            })

    return results

def parse_wilson_caption(caption: str) -> dict:
    """
    '2s 3m 3L' → {'S': 2, 'M': 3, 'L': 3}
    '1m'       → {'M': 1}
    """
    size_map = {}
    pattern = re.compile(r'(\d+)(xs|s|m|l|xl|xxl|3l|2xl)', re.IGNORECASE)
    for sl, size in pattern.findall(caption):
        size_map[size.upper()] = int(sl)
    return size_map

def looks_like_nike_adidas(text: str) -> bool:
    """Kiểm tra text có chứa mã Nike/Adidas không."""
    # Mã bắt đầu bằng 2-4 chữ cái + số, theo sau là size+số
    pattern = re.compile(
        r'[a-zA-Z]{1,4}\d[\w\-]*\s+\d+(xs|s|m|l|xl|xxl|3l|2xl)',
        re.IGNORECASE
    )
    return bool(pattern.search(text))


# ═══════════════════════════════════════════════
#  TELEGRAM HANDLERS
# ═══════════════════════════════════════════════

async def notify_owner(context, msg: str):
    """Gửi thông báo riêng cho owner."""
    try:
        await context.bot.send_message(chat_id=OWNER_CHAT_ID, text=msg, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Cannot notify owner: {e}")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
        return

    chat_id = message.chat_id
    text = message.text.strip()

    # ── Mở phiên ──
    if is_session_open(text):
        tab_name = extract_session_name(text)
        try:
            ws = create_tab(tab_name)
            sessions[chat_id] = {"tab_name": tab_name, "ws": ws, "stt": 1}
            pending_tracking[chat_id] = []
            await notify_owner(context,
                f"📦 *Mở phiên:* `{tab_name}`\nĐã tạo tab mới trong Google Sheet.")
        except Exception as e:
            await notify_owner(context, f"❌ Lỗi tạo tab `{tab_name}`: {e}")
        return

    # ── Đóng phiên ──
    if is_session_close(text):
        if chat_id in sessions:
            tab = sessions[chat_id]["tab_name"]
            stt = sessions[chat_id]["stt"] - 1
            del sessions[chat_id]
            pending_tracking.pop(chat_id, None)
            await notify_owner(context,
                f"✅ *Đóng phiên:* `{tab}`\nTổng: *{stt} dòng* đã ghi vào sheet.")
        return

    # ── Không có phiên đang mở ──
    if chat_id not in sessions:
        return

    session = sessions[chat_id]
    ws = session["ws"]

    # ── Mã Nike/Adidas ──
    if looks_like_nike_adidas(text):
        items = parse_nike_adidas_caption(text)
        if not items:
            return

        trackings = pending_tracking.get(chat_id, [])
        rows_added = []

        if trackings:
            # Nếu có nhiều tracking → mỗi tracking tạo bộ dòng riêng
            for tracking in trackings:
                for item in items:
                    row = [
                        session["stt"],
                        item["hang"],
                        item["ma_sp"],
                        "-",
                        item["size"],
                        item["sl"],
                        tracking,
                        "-",
                    ]
                    ws.append_row(row)
                    rows_added.append(row)
                    session["stt"] += 1
        else:
            for item in items:
                row = [
                    session["stt"],
                    item["hang"],
                    item["ma_sp"],
                    "-",
                    item["size"],
                    item["sl"],
                    "-",
                    "-",
                ]
                ws.append_row(row)
                rows_added.append(row)
                session["stt"] += 1

        # Reset tracking sau khi dùng
        pending_tracking[chat_id] = []

        lines = [f"✅ *Đã ghi {len(rows_added)} dòng* vào `{session['tab_name']}`:"]
        for r in rows_added:
            lines.append(f"• `{r[2]}` | {r[4]} x{r[5]} | Track: `{r[6]}`")
        await notify_owner(context, "\n".join(lines))


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.photo:
        return

    chat_id = message.chat_id
    caption = (message.caption or "").strip()

    # Tải ảnh chất lượng cao nhất
    photo = message.photo[-1]
    file  = await context.bot.get_file(photo.file_id)
    image_bytes = bytes(await file.download_as_bytearray())

    # ── Không có phiên: bỏ qua ──
    if chat_id not in sessions:
        return

    session = sessions[chat_id]
    ws      = session["ws"]

    # ── Wilson: caption dạng "2s 3m" hoặc "1m" ──
    wilson_caption = parse_wilson_caption(caption)
    nike_adidas_in_caption = looks_like_nike_adidas(caption)

    if wilson_caption and not nike_adidas_in_caption:
        # Wilson flow: đọc tag + tracking từ ảnh, size/SL từ caption
        try:
            data = gemini_wilson(image_bytes)
        except Exception as e:
            await notify_owner(context, f"❌ Gemini lỗi (Wilson): {e}")
            return

        ma_sp   = data.get("ma_sp") or "?"
        mau     = data.get("mau") or "-"
        tracking     = data.get("tracking") or "-"
        order_number = data.get("order_number") or "-"

        rows_added = []
        for size, sl in wilson_caption.items():
            row = [
                session["stt"],
                "Wilson",
                ma_sp,
                mau,
                size,
                sl,
                tracking,
                order_number,
            ]
            ws.append_row(row)
            rows_added.append(row)
            session["stt"] += 1

        lines = [f"✅ *Wilson* — {len(rows_added)} dòng vào `{session['tab_name']}`:"]
        for r in rows_added:
            lines.append(f"• `{r[2]}` | {r[3]} | {r[4]} x{r[5]} | Track: `{r[6]}`")
        await notify_owner(context, "\n".join(lines))

    else:
        # Nike/Adidas flow: đọc tracking từ ảnh, lưu tạm chờ text
        try:
            trackings = gemini_tracking(image_bytes)
        except Exception as e:
            await notify_owner(context, f"❌ Gemini lỗi (tracking): {e}")
            return

        if trackings:
            pending_tracking[chat_id] = trackings
            track_str = ", ".join(f"`{t}`" for t in trackings)
            await notify_owner(context,
                f"📷 *Ảnh nhận được* — tracking: {track_str}\n"
                f"⏳ Chờ text mã sản phẩm...")
        else:
            await notify_owner(context,
                "📷 *Ảnh nhận được* — không đọc được tracking.\n"
                "⏳ Chờ text mã sản phẩm...")


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info("✅ Bot started!")
    app.run_polling()


if __name__ == "__main__":
    main()

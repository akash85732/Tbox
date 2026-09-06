import logging
import re
import asyncio
import os
import tempfile
import requests
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes
from telegram.constants import ParseMode
from telethon import TelegramClient

# ─── CONFIG ───────────────────────────────────────────────────────────────────
BOT_TOKEN  = "8725658992:AAGMoq1Y0IFZL39mjKJGRJ5fOtCjfrMFcwo"
API_URL    = "https://flowvideoplayer.com/search/video"
SITE_URL   = "https://flowvideoplayer.com"

# MTProto credentials + the bot token reused for MTProto login
API_ID     = 33197883
API_HASH   = "681958d891ced37a4534df9490e4fe2c"

# Bot API hard limit for URL-fetch sends is 50 MB. Above this we must
# download on the server and re-upload via MTProto (supports up to ~2 GB).
URL_SEND_LIMIT = 50 * 1024 * 1024

LINK_PATTERN = re.compile(
    r"https?://(terasharefile\.com|terafileshare\.com|terabox\.com|www\.terabox\.com"
    r"|teraboxapp\.com|1024terabox\.com|1024tera\.com|4funbox\.com"
    r"|mirrobox\.com|nephobox\.com|freeterabox\.com|diskwala\.com"
    r"|terafileshare\.com)/\S+",
    re.IGNORECASE
)

BROWSER_UA = (
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Mobile Safari/537.36"
)

# ─── LOGGING ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


# ─── FRESH SESSION + CSRF PER REQUEST ────────────────────────────────────────
def fresh_session_and_csrf() -> tuple[requests.Session, str] | None:
    """
    Creates a NEW session, GETs the page to grab cookies + CSRF token.
    Returns (session, csrf_token) or None on failure.
    Both come from the SAME session so cookies match the token.
    """
    session = requests.Session()
    session.headers.update({
        "User-Agent": BROWSER_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
    })

    try:
        resp = session.get(SITE_URL, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        logger.error(f"Page fetch failed: {e}")
        return None

    # Extract CSRF token
    soup = BeautifulSoup(resp.text, "html.parser")
    csrf = None

    # Method 1: <meta name="csrf-token" content="...">
    meta = soup.find("meta", {"name": "csrf-token"})
    if meta and meta.get("content"):
        csrf = meta["content"]

    # Method 2: XSRF-TOKEN cookie
    if not csrf:
        xsrf = session.cookies.get("XSRF-TOKEN")
        if xsrf:
            csrf = requests.utils.unquote(xsrf)

    if not csrf:
        logger.error("CSRF token not found in page")
        return None

    # Step 1.5: device/init — site requires a fingerprint verification
    # before /search/video will respond (else "Direct access blocked" /
    # "Device token missing"). The verification is stored in the session.
    try:
        init_headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "X-CSRF-TOKEN": csrf,
            "User-Agent": BROWSER_UA,
            "Referer": SITE_URL + "/",
            "Origin": SITE_URL,
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
        }
        session.post(
            SITE_URL + "/device/init",
            json={
                "cpu": 8,
                "memory": 8,
                "touch": 0,
                "platform": "Linux x86_64",
                "lang": "en-US",
                "vendor": "Google Inc.",
                "webgl_vendor": None,
                "webgl_renderer": None,
                "ua": BROWSER_UA,
                "backup_token": None,
                "os": "linux",
                "browser": "chrome",
                "pwa_installed": False,
            },
            headers=init_headers,
            timeout=30,
        )
    except Exception as e:
        logger.warning(f"device/init failed (continuing): {e}")

    logger.info(f"Fresh session + CSRF: {csrf[:16]}...")
    return session, csrf


# ─── API CALL ─────────────────────────────────────────────────────────────────
def fetch_video_info(url: str) -> dict | None:
    """
    Fresh CSRF + session per call. Retries once on 419.
    Returns first video dict from response array.
    """
    for attempt in range(2):
        ctx = fresh_session_and_csrf()
        if not ctx:
            logger.error("Could not get session/CSRF")
            continue

        session, csrf = ctx

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "X-CSRF-TOKEN": csrf,
            "User-Agent": BROWSER_UA,
            "Referer": SITE_URL + "/",
            "Origin": SITE_URL,
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
        }

        try:
            resp = session.post(
                API_URL,
                json={"url": url},
                headers=headers,
                timeout=30
            )

            if resp.status_code == 419:
                logger.warning("419 CSRF mismatch — retrying with fresh token...")
                continue

            data = resp.json()

            if data.get("code") == 200 and data.get("status") and data.get("response"):
                return data["response"][0], session, csrf

            logger.warning(f"API: {data.get('message')} | HTTP {resp.status_code}")

        except Exception as e:
            logger.error(f"API error (attempt {attempt+1}): {e}")

    return None


# ─── MTProto (for files >50 MB that the Bot API URL path can't deliver) ───────
mtproto = TelegramClient("tera2_mtproto_session", API_ID, API_HASH)
_mtproto_started = False

async def _ensure_mtproto():
    """Start the MTProto client once, logged in as the bot itself."""
    global _mtproto_started
    if not _mtproto_started:
        await mtproto.start(bot_token=BOT_TOKEN)
        _mtproto_started = True
        logger.info("MTProto client started ✅")

async def mtproto_send(chat, path, caption, file_name):
    """Upload the downloaded file via MTProto (bypasses the 50 MB Bot API cap)."""
    await _ensure_mtproto()
    await mtproto.send_file(
        chat,
        path,
        caption=caption,
        file_name=file_name,
        supports_streaming=True,
    )
    logger.info("Uploaded to Telegram OK (MTProto)")


# ─── HANDLERS ─────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *TeraBox Downloader Bot*\n\n"
        "Koi bhi TeraBox ya TeraShare link bhejo — main seedha video send kar dunga! ⚡\n\n"
        "*Supported domains:*\n"
        "• `terasharefile.com` / `terafileshare.com`\n"
        "• `terabox.com` / `teraboxapp.com`\n"
        "• `1024tera.com` / `1024terabox.com`\n"
        "• `4funbox.com` / `mirrobox.com`\n"
        "• `nephobox.com` / `freeterabox.com`\n"
        "• `diskwala.com`",
        parse_mode=ParseMode.MARKDOWN
    )


def download_via_session(session, download_url, dest):
    """Download the resolved file through the session that owns the token."""
    resp = session.get(
        download_url,
        headers={"User-Agent": BROWSER_UA, "Referer": SITE_URL + "/"},
        stream=True,
        timeout=120,
    )
    resp.raise_for_status()
    with open(dest, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            if chunk:
                f.write(chunk)
    logger.info(f"Downloaded bytes to {dest}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    text = message.text or ""

    match = LINK_PATTERN.search(text)
    if not match:
        return

    url = match.group(0)
    processing_msg = await message.reply_text("⏳ Processing link...")

    result = fetch_video_info(url)

    if not result:
        await processing_msg.edit_text(
            "❌ Video fetch nahi ho saka. Link check karo ya baad mein try karo."
        )
        return

    info, session, csrf = result

    file_name    = info.get("file_name", "Video")
    file_size    = info.get("file_size", "?")
    file_size_b  = int(info.get("file_size_bytes") or 0)
    download_url = info.get("download_url", "")

    if not download_url:
        await processing_msg.edit_text("❌ Download URL nahi mila. Link invalid ho sakta hai.")
        return

    caption = (
        f"🎬 *{file_name}*\n"
        f"💾 Size: `{file_size}`\n"
        f"🔗 [Original Link]({url})"
    )

    # ── Path A: small files (≤50 MB) ──
    # Hand the resolved direct URL to Telegram. Telegram's own servers fetch
    # and deliver the video — zero server-side download by us.
    if file_size_b and file_size_b <= URL_SEND_LIMIT:
        await processing_msg.edit_text("📤 Telegram ko video bhej raha hoon...")
        try:
            await message.reply_video(
                video=download_url,
                caption=caption,
                parse_mode=ParseMode.MARKDOWN,
                supports_streaming=True,
                read_timeout=60,
                write_timeout=60,
                connect_timeout=30,
                filename=file_name + ".mp4",
            )
            logger.info("Sent direct URL to Telegram ✅")
            return
        except Exception as e:
            logger.error(f"Telegram URL send failed: {e}")
            await message.reply_text(
                f"⚠️ Auto-send fail ho gaya. Link khol lo:\n\n"
                f"📥 `{download_url}`\n\n"
                f"💾 Size: {file_size}",
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True,
            )
            return

    # ── Path B: big files (>50 MB) ──
    # Bot API can't URL-deliver >50 MB, so we download locally and re-upload
    # via MTProto (handles up to ~2 GB), then delete the temp file.
    await processing_msg.edit_text("📥 Bada file hai — server se lekar MTProto se bhej rahi hoon...")
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=".mp4")
        os.close(fd)
        download_via_session(session, download_url, tmp_path)
        await mtproto_send(message.chat_id, tmp_path, caption, file_name + ".mp4")
        logger.info("Big file delivered via MTProto ✅")
    except Exception as e:
        logger.error(f"MTProto path failed: {e}")
        await message.reply_text(
            f"⚠️ Bada file send fail. Link khol lo:\n\n"
            f"📥 `{download_url}`\n\n"
            f"💾 Size: {file_size}",
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
        )
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Bot started ✅")
    app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    asyncio.set_event_loop(asyncio.new_event_loop())
    main()

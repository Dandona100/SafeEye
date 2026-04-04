"""
Minimal Telegram bot with SafeEye content scanning.
Scans every photo sent to the bot before forwarding.

Usage:
    pip install python-telegram-bot aiohttp
    TELEGRAM_TOKEN=xxx SAFEEYE_URL=http://localhost:1985 SAFEEYE_TOKEN=yyy python telegram_bot.py
"""
import os
import aiohttp
import tempfile
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

SAFEEYE_URL = os.environ.get("SAFEEYE_URL", "http://localhost:1985")
SAFEEYE_TOKEN = os.environ["SAFEEYE_TOKEN"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]


async def scan_file(file_path: str) -> dict:
    """Scan a file with SafeEye."""
    async with aiohttp.ClientSession() as session:
        with open(file_path, "rb") as f:
            data = aiohttp.FormData()
            data.add_field("file", f, filename="photo.jpg")
            async with session.post(
                f"{SAFEEYE_URL}/api/v1/scan/file",
                data=data,
                headers={"Authorization": f"Bearer {SAFEEYE_TOKEN}"},
            ) as resp:
                return await resp.json()


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming photos — scan before allowing."""
    photo = update.message.photo[-1]  # Highest resolution
    file = await context.bot.get_file(photo.file_id)

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        await file.download_to_drive(tmp.name)
        result = await scan_file(tmp.name)
        os.unlink(tmp.name)

    scan = result.get("result", {})
    if scan.get("is_nsfw"):
        labels = ", ".join(scan.get("labels", []))
        await update.message.reply_text(f"🚫 Content blocked by SafeEye\nDetected: {labels}")
        await update.message.delete()
    else:
        await update.message.reply_text("✅ Content is safe")


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    print("Bot running with SafeEye scanning...")
    app.run_polling()


if __name__ == "__main__":
    main()

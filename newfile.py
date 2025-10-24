import os
import asyncio
from telethon import TelegramClient, events
from yt_dlp import YoutubeDL
from tqdm import tqdm

# ───────── CONFIG ─────────
api_id = 1517443
api_hash = "9e6f52ee11f7a2efd6c79d47e5e984e6"
BOT_TOKEN = "7618021836:AAG_VYCXJ6IP9mcQYwuaIaqRiQ2pP29ij6U"

DOWNLOAD_DIR = "./downloads"
# ─────────────────────────

bot = TelegramClient("bot_session", api_id, api_hash).start(bot_token=BOT_TOKEN)

def human_readable(size):
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"

async def upload_with_progress(file_path, chat_id):
    file_size = os.path.getsize(file_path)
    filename = os.path.basename(file_path)
    with tqdm(total=file_size, unit='B', unit_scale=True, desc=f"Uploading {filename}") as pbar:
        async def progress_callback(current, total):
            pbar.n = current
            pbar.refresh()
        await bot.send_file(
            chat_id,
            file_path,
            caption=f"✅ Uploaded: {filename}\n📦 Size: {human_readable(file_size)}",
            progress_callback=progress_callback
        )

@bot.on(events.NewMessage(pattern=r"^/dl\s+(.+)"))
async def handler(event):
    url = event.pattern_match.group(1)
    await event.reply("📥 Downloading...")

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    ydl_opts = {
        "outtmpl": f"{DOWNLOAD_DIR}/%(title).80s.%(ext)s",
        "format": "bestvideo+bestaudio/best",
        "quiet": True,
    }

    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)

    await event.reply("✅ Download complete. Uploading...")
    await upload_with_progress(filename, event.chat_id)
    await event.reply("✅ Done!")

print("🤖 Bot started — use /dl <url> to download & upload (max 2 GB)")
bot.run_until_disconnected()
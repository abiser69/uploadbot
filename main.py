# mai_ptb21_resilient_unique.py
import os
import json
import zipfile
import subprocess
import asyncio
import random
import string
from datetime import datetime
from pathlib import Path
from PIL import Image
from telegram import InputSticker
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    ContextTypes,
    filters,
)
from telegram.error import NetworkError, RetryAfter, TimedOut, BadRequest

# ========= CONFIG (your details) =========
BOT_TOKEN = "7618021836:AAGDMfpv93YeNS_ZHIfI3lQdR6VDagJc8BU"
USER_ID = 6048171967  # your Telegram numeric ID

# ========= CONVERSION HELPERS =========
def convert_static(input_path: str, output_path: str) -> None:
    """Static -> WEBP, max 512px, compressed to reduce upload time/size."""
    im = Image.open(input_path).convert("RGBA")
    im.thumbnail((512, 512))
    im.save(output_path, "WEBP", quality=85, method=6)

def convert_animated(input_path: str, output_path: str) -> None:
    """GIF -> WEBM(VP9), <=512px, 30fps, 3s, moderate bitrate."""
    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-vf", "scale=512:-1:force_original_aspect_ratio=decrease,fps=30",
        "-c:v", "libvpx-vp9",
        "-b:v", "512K",
        "-deadline", "realtime",
        "-an",
        "-t", "3",
        output_path,
    ]
    subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

# ========= SAFE TELEGRAM CALLS WITH RETRY =========
async def _sleep_backoff(base: float, attempt: int):
    await asyncio.sleep(base * (2 ** attempt))

async def safe_create_set(bot, user_id, name, title, first_input, first_fmt):
    """
    Try create_new_sticker_set across PTB 21.x variants and with retries.
    Also surfaces 'name occupied' quickly for the caller to try a new name.
    """
    attempts = 3
    for i in range(attempts):
        try:
            # 1) No explicit format kwarg
            return await bot.create_new_sticker_set(
                user_id=user_id,
                name=name,
                title=title,
                stickers=[first_input],
            )
        except TypeError:
            # 2) sticker_type
            try:
                return await bot.create_new_sticker_set(
                    user_id=user_id,
                    name=name,
                    title=title,
                    stickers=[first_input],
                    sticker_type=first_fmt,
                )
            except TypeError:
                # 3) sticker_format
                return await bot.create_new_sticker_set(
                    user_id=user_id,
                    name=name,
                    title=title,
                    stickers=[first_input],
                    sticker_format=first_fmt,
                )
        except RetryAfter as e:
            await asyncio.sleep(getattr(e, "retry_after", 3))
        except BadRequest as e:
            # If the name is occupied, bubble up so caller can try another name.
            msg = str(e).lower()
            if "name is already occupied" in msg or "sticker set name is already occupied" in msg:
                raise e
            # Other 400s: brief backoff then retry
            await _sleep_backoff(0.5, i)
        except (NetworkError, TimedOut):
            await _sleep_backoff(1.0, i)
        except Exception:
            await _sleep_backoff(0.5, i)
    raise RuntimeError("create_new_sticker_set failed after retries")

async def safe_add_sticker(bot, user_id, name, st: InputSticker):
    """
    Add one sticker with retries/backoff and flood-wait handling.
    """
    attempts = 6
    for i in range(attempts):
        try:
            return await bot.add_sticker_to_set(
                user_id=user_id,
                name=name,
                sticker=st,
            )
        except RetryAfter as e:
            await asyncio.sleep(getattr(e, "retry_after", 3))
        except (NetworkError, TimedOut):
            await _sleep_backoff(1.0, i)
        except Exception:
            await _sleep_backoff(0.5, i)
    raise RuntimeError("add_sticker_to_set failed after retries")

# ========= NAME GENERATOR (avoids collisions, keeps required suffix) =========
def _rand(n=5):
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))

def build_candidate_names(pack_name_base: str, bot_username: str, bot_id: int, max_candidates: int = 20):
    """
    Telegram requires: only a-z0-9_, 1-64 chars, and must end with _by_<botusername>.
    We generate multiple unique candidates by inserting a suffix BEFORE the _by_<botusername>.
    """
    base = "".join(c if c.isalnum() or c == "_" else "_" for c in pack_name_base.lower())
    suffixes = [
        "",                                   # plain base
        f"{bot_id}",                          # bot id
        datetime.now().strftime("%Y%m%d%H%M%S"),  # timestamp
        _rand(4),
        _rand(5),
        "v2", "v3", "v4",
    ]
    # extend with more randoms if needed
    while len(suffixes) < max_candidates:
        suffixes.append(_rand(5))

    candidates = []
    tail = f"_by_{bot_username.lower()}"
    for s in suffixes:
        mid = f"_{s}" if s else ""
        # ensure whole name <= 64 chars
        # name = <trimmed_base><mid><tail>
        budget_for_base = 64 - len(mid) - len(tail)
        trimmed_base = base[:max(1, budget_for_base)]
        candidates.append(f"{trimmed_base}{mid}{tail}")
    # de-dupe while preserving order
    seen = set()
    uniq = []
    for n in candidates:
        if n not in seen:
            uniq.append(n)
            seen.add(n)
    return uniq[:max_candidates]

# ========= BOT HANDLERS =========
async def start(update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send me a .zip or .wastickers file (WhatsApp pack) and I’ll convert it into a Telegram sticker pack."
    )

async def handle_file(update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc:
        await update.message.reply_text("Please send a .zip or .wastickers file.")
        return

    tg_file = await doc.get_file()
    filename = doc.file_name or "stickers.zip"
    await tg_file.download_to_drive(filename)

    await update.message.reply_text("Processing your sticker pack... ⏳")
    await convert_pack(filename, update, context)

# ========= CORE =========
async def convert_pack(zip_path: str, update, context: ContextTypes.DEFAULT_TYPE):
    temp_dir = Path("temp_pack")
    temp_dir.mkdir(exist_ok=True)

    try:
        with zipfile.ZipFile(zip_path, "r") as z:
            z.extractall(temp_dir)
    except Exception as e:
        await update.message.reply_text(f"Could not open archive: {e}")
        cleanup(zip_path, temp_dir)
        return

    metadata_path = temp_dir / "metadata.json"
    if metadata_path.exists():
        try:
            with open(metadata_path, "r", encoding="utf-8") as f:
                metadata = json.load(f)
        except Exception:
            metadata = {"name": "ConvertedPack", "author": "Unknown"}
    else:
        metadata = {"name": "ConvertedPack", "author": "Unknown"}

    pack_title = (str(metadata.get("name", "ConvertedPack")) or "ConvertedPack").strip()
    author = str(metadata.get("author", "Unknown"))
    pack_name_base = pack_title.replace(" ", "_")

    files = sorted(
        f for f in os.listdir(temp_dir)
        if f.lower().endswith((".webp", ".png", ".gif"))
    )
    if not files:
        await update.message.reply_text(
            "No valid stickers found. Supported inside the archive: .webp, .png, .gif"
        )
        cleanup(zip_path, temp_dir)
        return

    converted = []
    for i, fname in enumerate(files):
        src = str(temp_dir / fname)
        ext = Path(fname).suffix.lower()
        dst = str(temp_dir / (f"tg_{i}.webm" if ext == ".gif" else f"tg_{i}.webp"))
        try:
            if ext == ".gif":
                convert_animated(src, dst)
            else:
                convert_static(src, dst)
            converted.append(dst)
        except Exception as e:
            print(f"[WARN] Conversion failed for {src}: {e}")

    if not converted:
        await update.message.reply_text("Conversion failed for all files.")
        cleanup(zip_path, temp_dir)
        return

    me = await context.bot.get_me()
    candidates = build_candidate_names(pack_name_base, me.username, me.id, max_candidates=20)

    first = converted[0]
    first_fmt = "video" if first.endswith(".webm") else "static"
    first_input = InputSticker(
        sticker=open(first, "rb"),
        emoji_list=["😄"],
        format=first_fmt,
    )

    # Try many names until one works
    created = False
    chosen_name = None
    last_err = None
    for name in candidates:
        try:
            await safe_create_set(
                context.bot,
                USER_ID,
                name,
                f"{pack_title} (by {author})",
                first_input,
                first_fmt,
            )
            created, chosen_name = True, name
            break
        except BadRequest as e:
            # If occupied, continue to next candidate
            msg = str(e).lower()
            if "name is already occupied" in msg or "sticker set name is already occupied" in msg:
                continue
            last_err = e
        except Exception as e:
            last_err = e

    if not created:
        await update.message.reply_text(f"Failed to create sticker pack: {last_err}")
        cleanup(zip_path, temp_dir)
        return

    # Add remaining stickers with retries + small throttle
    for idx, path in enumerate(converted[1:], start=2):
        try:
            fmt = "video" if path.endswith(".webm") else "static"
            st = InputSticker(sticker=open(path, "rb"), emoji_list=["🙂"], format=fmt)
            await safe_add_sticker(context.bot, USER_ID, chosen_name, st)
        except Exception as e:
            print(f"[WARN] Failed to add {path}: {e}")
        # Gentle throttle to avoid flood/timeouts
        await asyncio.sleep(0.8)
        if idx % 10 == 0:
            await asyncio.sleep(2.0)

    await update.message.reply_text(f"✅ Sticker pack created: https://t.me/addstickers/{chosen_name}")
    cleanup(zip_path, temp_dir)

# ========= CLEANUP =========
def cleanup(zip_path: str | Path, temp_dir: Path) -> None:
    try:
        for f in temp_dir.iterdir():
            try:
                f.unlink()
            except Exception:
                pass
        temp_dir.rmdir()
    except Exception:
        pass
    try:
        Path(zip_path).unlink()
    except Exception:
        pass

# ========= BOOT =========
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(
        MessageHandler(
            filters.Document.FileExtension("zip") | filters.Document.FileExtension("wastickers"),
            handle_file,
        )
    )
    app.run_polling()

if __name__ == "__main__":
    main()

import os
import json
import zipfile
import subprocess
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

# ========= CONFIG (your details) =========
BOT_TOKEN = "7618021836:AAGDMfpv93YeNS_ZHIfI3lQdR6VDagJc8BU"
USER_ID = 6048171967  # your Telegram numeric ID

# ========= CONVERSION HELPERS =========
def convert_static(input_path: str, output_path: str) -> None:
    im = Image.open(input_path).convert("RGBA")
    im.thumbnail((512, 512))
    im.save(output_path, "WEBP")

def convert_animated(input_path: str, output_path: str) -> None:
    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-vf", "scale=512:-1:force_original_aspect_ratio=decrease,fps=30",
        "-c:v", "libvpx-vp9",
        "-b:v", "512K",
        "-an",
        "-t", "3",
        output_path,
    ]
    subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

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
    base_set_name = f"{pack_name_base}_by_{me.username}".lower()
    fallbacks = [base_set_name, f"{pack_name_base}_{me.id}_by_{me.username}".lower()]

    first = converted[0]
    first_fmt = "video" if first.endswith(".webm") else "static"

    first_input = InputSticker(
        sticker=open(first, "rb"),
        emoji_list=["😄"],
        format=first_fmt,
    )

    created = False
    chosen_name = None
    last_error = None

    # Adaptive create_new_sticker_set handling multiple PTB variants
    for name in fallbacks:
        try:
            # 1️⃣ Try plain
            await context.bot.create_new_sticker_set(
                user_id=USER_ID,
                name=name,
                title=f"{pack_title} (by {author})",
                stickers=[first_input],
            )
            created, chosen_name = True, name
            break
        except TypeError:
            # 2️⃣ Try sticker_type
            try:
                await context.bot.create_new_sticker_set(
                    user_id=USER_ID,
                    name=name,
                    title=f"{pack_title} (by {author})",
                    stickers=[first_input],
                    sticker_type=first_fmt,
                )
                created, chosen_name = True, name
                break
            except TypeError:
                # 3️⃣ Try sticker_format
                try:
                    await context.bot.create_new_sticker_set(
                        user_id=USER_ID,
                        name=name,
                        title=f"{pack_title} (by {author})",
                        stickers=[first_input],
                        sticker_format=first_fmt,
                    )
                    created, chosen_name = True, name
                    break
                except Exception as e3:
                    last_error = e3
            except Exception as e2:
                last_error = e2
        except Exception as e:
            last_error = e

    if not created:
        await update.message.reply_text(f"Failed to create sticker pack: {last_error}")
        cleanup(zip_path, temp_dir)
        return

    # Add remaining stickers
    for path in converted[1:]:
        try:
            fmt = "video" if path.endswith(".webm") else "static"
            st = InputSticker(sticker=open(path, "rb"), emoji_list=["🙂"], format=fmt)
            await context.bot.add_sticker_to_set(
                user_id=USER_ID,
                name=chosen_name,
                sticker=st,
            )
        except Exception as e:
            print(f"[WARN] Failed to add {path}: {e}")

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

"""
plugins/media.py
-----------------
Module B: Advanced Media Processing & Storage
  - Fast screenshot generator (FFmpeg, 4-10 evenly spaced frames)
  - Custom renamer & thumbnail engine (admin only)
  - Content-hash duplicate cleaner (SHA-256 for files, perceptual hash for images)
"""

import hashlib
import logging
import os
import shutil
import uuid

from hydrogram import Client, filters
from hydrogram.types import Message

from config import Config
from database import db
from utils.decorators import admin_only, group_only
from utils.ffmpeg_utils import extract_screenshots, generate_thumbnail, embed_thumbnail_no_reencode
from utils.helpers import safe_filename

logger = logging.getLogger("media")

try:
    import imagehash
    from PIL import Image
    HAS_IMAGEHASH = True
except ImportError:
    HAS_IMAGEHASH = False
    logger.warning("imagehash not installed; falling back to SHA-256 only for images.")


# =====================================================================
# 1. FAST SCREENSHOT GENERATOR
# =====================================================================

@Client.on_message(filters.command("ss") & (filters.reply | filters.video))
async def screenshot_cmd(client: Client, message: Message):
    target = message.reply_to_message if message.reply_to_message else message
    if not (target.video or target.document):
        await message.reply_text("Reply to a video (or send one with /ss) to generate screenshots.")
        return

    count = 6
    if len(message.command) > 1 and message.command[1].isdigit():
        count = max(4, min(int(message.command[1]), 10))

    status = await message.reply_text("⏳ Downloading video for screenshot extraction...")
    work_dir = os.path.join(Config.TMP_DOWNLOAD_DIR, uuid.uuid4().hex)
    os.makedirs(work_dir, exist_ok=True)

    try:
        video_path = await target.download(file_name=os.path.join(work_dir, "input.mp4"))
        await status.edit_text(f"🎬 Extracting {count} screenshots...")
        frames = await extract_screenshots(video_path, work_dir, count=count)

        if not frames:
            await status.edit_text("❌ Could not extract any frames from this video.")
            return

        media_group = []
        from hydrogram.types import InputMediaPhoto
        for i, frame_path in enumerate(frames):
            media_group.append(InputMediaPhoto(frame_path, caption="🖼 Screenshot" if i == 0 else None))

        await client.send_media_group(message.chat.id, media_group)
        await status.delete()
    except Exception as e:
        logger.exception("Screenshot generation failed")
        await status.edit_text(f"❌ Screenshot generation failed: {e}")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# =====================================================================
# 2. RENAMER & THUMBNAIL ENGINE (Admin Only)
# =====================================================================

@Client.on_message(filters.command("rename") & filters.reply)
@admin_only
async def rename_cmd(client: Client, message: Message):
    """
    Usage: reply to a media message with /rename <new_filename.ext>
    Optionally embeds a saved thumbnail without re-encoding the stream.
    """
    target = message.reply_to_message
    if not (target.video or target.document or target.audio):
        await message.reply_text("Reply to a video/document/audio file to rename it.")
        return
    if len(message.command) < 2:
        await message.reply_text("Usage: /rename <new_filename.ext>")
        return

    new_name = safe_filename(" ".join(message.command[1:]))
    status = await message.reply_text(f"⏳ Downloading & renaming to `{new_name}`...")

    work_dir = os.path.join(Config.TMP_DOWNLOAD_DIR, uuid.uuid4().hex)
    os.makedirs(work_dir, exist_ok=True)
    try:
        src_path = await target.download(file_name=os.path.join(work_dir, new_name))

        user = await db.get_user(message.from_user.id)
        thumb_path = (user or {}).get("saved_thumbnail_path")
        final_path = src_path

        if thumb_path and os.path.exists(thumb_path) and target.video:
            embedded_path = os.path.join(work_dir, f"embedded_{new_name}")
            ok = await embed_thumbnail_no_reencode(src_path, thumb_path, embedded_path)
            if ok:
                final_path = embedded_path

        await status.edit_text("⬆️ Uploading renamed file...")
        if target.video:
            await client.send_video(message.chat.id, final_path, file_name=new_name,
                                     caption=f"✅ Renamed to `{new_name}`")
        elif target.audio:
            await client.send_audio(message.chat.id, final_path, file_name=new_name,
                                     caption=f"✅ Renamed to `{new_name}`")
        else:
            await client.send_document(message.chat.id, final_path, file_name=new_name,
                                        caption=f"✅ Renamed to `{new_name}`")
        await status.delete()
    except Exception as e:
        logger.exception("Rename failed")
        await status.edit_text(f"❌ Rename failed: {e}")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


@Client.on_message(filters.command("setthumb") & filters.reply)
@admin_only
async def set_thumbnail_cmd(client: Client, message: Message):
    if not message.reply_to_message.photo:
        await message.reply_text("Reply to a photo with /setthumb to save it as your thumbnail template.")
        return

    thumb_dir = os.path.join(Config.TMP_DOWNLOAD_DIR, "thumbnails")
    os.makedirs(thumb_dir, exist_ok=True)
    thumb_path = os.path.join(thumb_dir, f"{message.from_user.id}.jpg")
    await message.reply_to_message.download(file_name=thumb_path)

    await db.users.update_one(
        {"_id": message.from_user.id},
        {"$set": {"saved_thumbnail_path": thumb_path}},
        upsert=True,
    )
    await message.reply_text("✅ Thumbnail template saved. It will be embedded on future /rename calls.")


@Client.on_message(filters.command("delthumb"))
@admin_only
async def del_thumbnail_cmd(client: Client, message: Message):
    user = await db.get_user(message.from_user.id)
    path = (user or {}).get("saved_thumbnail_path")
    if path and os.path.exists(path):
        os.remove(path)
    await db.users.update_one(
        {"_id": message.from_user.id}, {"$unset": {"saved_thumbnail_path": ""}}
    )
    await message.reply_text("🗑 Thumbnail template removed.")


# =====================================================================
# 3. CONTENT-HASH DUPLICATE CLEANER
# =====================================================================

def _sha256_of_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


async def _compute_hash(local_path: str, is_image: bool) -> str:
    if is_image and HAS_IMAGEHASH:
        try:
            return str(imagehash.phash(Image.open(local_path)))
        except Exception as e:
            logger.warning(f"phash failed, falling back to sha256: {e}")
    return _sha256_of_file(local_path)


@Client.on_message((filters.photo | filters.video | filters.document) & filters.group, group=5)
async def duplicate_watcher(client: Client, message: Message):
    chat = await db.get_chat(message.chat.id)
    if not chat.get("auto_duplicate_clean", False):
        return

    media_type = "photo" if message.photo else ("video" if message.video else "document")
    work_dir = os.path.join(Config.TMP_DOWNLOAD_DIR, uuid.uuid4().hex)
    os.makedirs(work_dir, exist_ok=True)
    try:
        local_path = await message.download(file_name=os.path.join(work_dir, "media"))
        file_hash = await _compute_hash(local_path, is_image=(media_type == "photo"))

        existing = await db.find_duplicate(message.chat.id, file_hash)
        if existing:
            try:
                await message.delete()
                logger.info(f"Deleted duplicate media in chat {message.chat.id} (hash={file_hash[:12]}...)")
            except Exception as e:
                logger.warning(f"Failed to delete duplicate: {e}")
        else:
            await db.store_hash(
                file_hash, message.chat.id,
                file_id=(message.photo or message.video or message.document).file_id,
                message_id=message.id, media_type=media_type,
            )
    except Exception as e:
        logger.warning(f"Duplicate check failed: {e}")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


@Client.on_message(filters.command("dupclean") & filters.group)
@admin_only
@group_only
async def toggle_dup_clean_cmd(client: Client, message: Message):
    chat = await db.get_chat(message.chat.id)
    new_state = not chat.get("auto_duplicate_clean", False)
    await db.update_chat(message.chat.id, {"auto_duplicate_clean": new_state})
    await message.reply_text(f"Duplicate auto-clean is now {'ON ✅' if new_state else 'OFF ❌'}.")

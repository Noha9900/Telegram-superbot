"""
plugins/broadcast.py
---------------------
Module D: Broadcast, Migration & Custom Layout Builder
  - /broadcast: send text/media to all users, with flexible inline button grid
    and optional APScheduler-based future scheduling
  - /migrate: high-speed channel-to-channel batch copier
"""

import asyncio
import logging
import re

from hydrogram import Client, filters
from hydrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from hydrogram.errors import RPCError, FloodWait

from database import db
from utils.decorators import owner_only

logger = logging.getLogger("broadcast")

# Button syntax embedded in the broadcast text, one row per line:
#   [Label1 - https://url1] [Label2 - https://url2]
#   [Label3 - https://url3]
BUTTON_ROW_RE = re.compile(r"\[([^\]\-]+)-\s*(https?://\S+?)\]")


def _parse_button_grid(text: str) -> tuple[str, InlineKeyboardMarkup | None]:
    """Strips button-definition lines out of the text and builds a keyboard."""
    lines = text.splitlines()
    body_lines = []
    rows = []
    for line in lines:
        matches = BUTTON_ROW_RE.findall(line)
        if matches:
            rows.append([InlineKeyboardButton(label.strip(), url=url.strip()) for label, url in matches])
        else:
            body_lines.append(line)
    body = "\n".join(body_lines).strip()
    return body, (InlineKeyboardMarkup(rows) if rows else None)


async def _do_broadcast(client: Client, source: Message, body: str, kb, status: Message):
    user_ids = await db.all_user_ids()
    sent, failed = 0, 0
    for uid in user_ids:
        try:
            if source.reply_to_message and (source.reply_to_message.photo
                                              or source.reply_to_message.video
                                              or source.reply_to_message.document
                                              or source.reply_to_message.poll):
                await source.reply_to_message.copy(uid, caption=body or None, reply_markup=kb)
            else:
                await client.send_message(uid, body, reply_markup=kb)
            sent += 1
        except FloodWait as e:
            await asyncio.sleep(e.value)
        except RPCError:
            failed += 1
        if sent % 50 == 0:
            try:
                await status.edit_text(f"📢 Broadcasting... sent {sent}, failed {failed} / {len(user_ids)}")
            except RPCError:
                pass
    await status.edit_text(f"✅ Broadcast complete. Sent: {sent}, Failed: {failed}, Total: {len(user_ids)}")


@Client.on_message(filters.command("broadcast"))
@owner_only
async def broadcast_cmd(client: Client, message: Message):
    """
    Usage:
      /broadcast <text with optional [Label - url] button rows>
      (reply to a media/poll message to broadcast that instead)
      /broadcast_at <YYYY-MM-DD HH:MM> <text...>   -- scheduled via APScheduler (see bot.py)
    """
    parts = message.text.split(None, 1)
    if len(parts) < 2 and not message.reply_to_message:
        await message.reply_text(
            "Usage: /broadcast <text>\nOptional buttons: [Label - https://url] per row.\n"
            "Reply to media/poll to broadcast that content instead."
        )
        return

    raw_text = parts[1] if len(parts) > 1 else (message.reply_to_message.caption or "")
    body, kb = _parse_button_grid(raw_text)

    status = await message.reply_text("📢 Starting broadcast...")
    asyncio.create_task(_do_broadcast(client, message, body, kb, status))


# ------------------------------------------------------------------ scheduling

@Client.on_message(filters.command("broadcast_at"))
@owner_only
async def broadcast_at_cmd(client: Client, message: Message):
    """Usage: /broadcast_at YYYY-MM-DD HH:MM <text with optional [Label - url] rows>"""
    from datetime import datetime, timezone
    from bot import schedule_broadcast  # local import avoids circular import at module load

    parts = message.text.split(None, 3)
    if len(parts) < 4:
        await message.reply_text("Usage: /broadcast_at YYYY-MM-DD HH:MM <text>")
        return
    _, date_str, time_str, raw_text = parts
    try:
        run_at = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        await message.reply_text("Invalid date/time. Format: YYYY-MM-DD HH:MM (UTC)")
        return
    if run_at <= datetime.now(timezone.utc):
        await message.reply_text("That time is in the past.")
        return

    body, kb = _parse_button_grid(raw_text)
    schedule_broadcast(run_at, message, body, kb)
    await message.reply_text(f"⏰ Broadcast scheduled for {run_at.strftime('%Y-%m-%d %H:%M')} UTC.")


# =====================================================================
# CHANNEL-TO-CHANNEL DATA MIGRATOR
# =====================================================================

@Client.on_message(filters.command("migrate"))
@owner_only
async def migrate_cmd(client: Client, message: Message):
    """Usage: /migrate <source_chat_id> <target_chat_id> [limit]"""
    if len(message.command) < 3:
        await message.reply_text("Usage: /migrate <source_chat_id> <target_chat_id> [limit]")
        return

    try:
        source_id = int(message.command[1])
        target_id = int(message.command[2])
        limit = int(message.command[3]) if len(message.command) > 3 else None
    except ValueError:
        await message.reply_text("Chat IDs and limit must be integers.")
        return

    status = await message.reply_text("🚚 Starting migration...")
    copied, failed = 0, 0
    async for msg in client.get_chat_history(source_id, limit=limit or 0):
        if msg.service or msg.empty:
            continue
        try:
            await msg.copy(target_id)
            copied += 1
        except FloodWait as e:
            await asyncio.sleep(e.value)
        except RPCError as e:
            failed += 1
            logger.debug(f"Migrate skip msg {msg.id}: {e}")
        if copied % 100 == 0 and copied:
            try:
                await status.edit_text(f"🚚 Migrating... copied {copied}, failed {failed}")
            except RPCError:
                pass

    await status.edit_text(f"✅ Migration complete. Copied: {copied}, Failed: {failed}")

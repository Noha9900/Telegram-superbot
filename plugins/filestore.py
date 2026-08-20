"""
plugins/filestore.py
---------------------
Module B.4: High-Speed FileStore Link Engine
  - Admin forwards/uploads file(s) -> stored in private DB channel
  - Encoded permanent deep-link generated: https://t.me/Bot?start=<token>
  - Public/private toggle, revocation, and fast multi-chunk delivery via /start
"""

import logging

from hydrogram import Client, filters
from hydrogram.types import Message
from hydrogram.errors import RPCError

from config import Config
from database import db
from utils.decorators import admin_only
from utils.helpers import encode_token, decode_token, gen_random_token

logger = logging.getLogger("filestore")

# in-memory batch staging: admin_id -> list[message_ids in DB channel]
_batch_sessions: dict[int, list[int]] = {}


@Client.on_message(filters.command("batch"))
@admin_only
async def batch_start_cmd(client: Client, message: Message):
    _batch_sessions[message.from_user.id] = []
    await message.reply_text(
        "📥 Batch mode started. Forward/send the files you want to store, "
        "then send /done to generate the link, or /cancelbatch to abort."
    )


@Client.on_message(filters.command("cancelbatch"))
@admin_only
async def batch_cancel_cmd(client: Client, message: Message):
    _batch_sessions.pop(message.from_user.id, None)
    await message.reply_text("❌ Batch cancelled.")


@Client.on_message(
    filters.private & (filters.document | filters.video | filters.photo | filters.audio)
)
@admin_only
async def collect_file_cmd(client: Client, message: Message):
    """While in an active /batch session, silently forward incoming files to the DB channel."""
    if message.from_user.id not in _batch_sessions:
        return  # not in batch mode -> ignore (regular file store still handled by /link below)
    try:
        stored = await message.copy(Config.DB_CHANNEL_ID)
        _batch_sessions[message.from_user.id].append(stored.id)
        await message.reply_text(
            f"✅ Added ({len(_batch_sessions[message.from_user.id])} so far). Send more, or /done."
        )
    except RPCError as e:
        await message.reply_text(f"❌ Failed to store file: {e}")


@Client.on_message(filters.command("done"))
@admin_only
async def batch_done_cmd(client: Client, message: Message):
    ids = _batch_sessions.pop(message.from_user.id, None)
    if not ids:
        await message.reply_text("No active batch (or no files added). Use /batch first.")
        return

    token = gen_random_token(12)
    await db.create_filestore_entry(token, ids, message.from_user.id, is_public=True)

    me = await client.get_me()
    link = f"https://t.me/{me.username}?start=batch_{token}"
    await message.reply_text(
        f"✅ Batch stored ({len(ids)} file(s)).\n\n🔗 Link:\n{link}\n\n"
        f"Use /revoke {token} to disable it later."
    )


@Client.on_message(filters.command("link") & filters.reply)
@admin_only
async def single_link_cmd(client: Client, message: Message):
    target = message.reply_to_message
    if not (target.document or target.video or target.photo or target.audio):
        await message.reply_text("Reply to a media message with /link to generate a store link.")
        return
    try:
        stored = await target.copy(Config.DB_CHANNEL_ID)
    except RPCError as e:
        await message.reply_text(f"❌ Failed to store file: {e}")
        return

    token = gen_random_token(12)
    await db.create_filestore_entry(token, [stored.id], message.from_user.id, is_public=True)

    me = await client.get_me()
    link = f"https://t.me/{me.username}?start=file_{token}"
    await message.reply_text(f"🔗 Link generated:\n{link}")


@Client.on_message(filters.command("revoke"))
@admin_only
async def revoke_cmd(client: Client, message: Message):
    if len(message.command) < 2:
        await message.reply_text("Usage: /revoke <token>")
        return
    token = message.command[1]
    await db.revoke_filestore_entry(token)
    await message.reply_text(f"🚫 Token `{token}` revoked. Existing links will stop working.")


@Client.on_message(filters.command("start") & filters.private)
async def start_cmd(client: Client, message: Message):
    await db.add_user(message.from_user.id, message.from_user.first_name, message.from_user.username)

    if len(message.command) < 2:
        await message.reply_text(
            "👋 Hi! I'm your Community Super-Bot.\n"
            "Use me inside a group for moderation, VIP subscriptions, and file sharing."
        )
        return

    payload = message.command[1]
    if not (payload.startswith("batch_") or payload.startswith("file_")):
        return

    token = payload.split("_", 1)[1]
    entry = await db.get_filestore_entry(token)
    if not entry:
        await message.reply_text("❌ This link is invalid or has expired.")
        return
    if entry.get("is_revoked"):
        await message.reply_text("🚫 This link has been revoked by the admin.")
        return
    if not entry.get("is_public") and message.from_user.id != entry.get("owner_id"):
        await message.reply_text("🔒 This is a private link. Access denied.")
        return

    status = await message.reply_text(f"📦 Delivering {len(entry['file_ids'])} file(s)...")
    delivered = 0
    for msg_id in entry["file_ids"]:
        try:
            await client.copy_message(message.chat.id, Config.DB_CHANNEL_ID, msg_id)
            delivered += 1
        except RPCError as e:
            logger.warning(f"Failed to deliver file {msg_id} for token {token}: {e}")
    await status.edit_text(f"✅ Delivered {delivered}/{len(entry['file_ids'])} file(s).")

"""
plugins/analytics.py
---------------------
Module E: Member Tracking & Deep Analytics
  - Passive activity tracker (message frequency, media counters, active hours)
  - /audit: comprehensive statistical report for a group/channel
"""

import logging
from datetime import datetime, timedelta, timezone

from hydrogram import Client, filters
from hydrogram.types import Message
from hydrogram.errors import RPCError

from database import db
from utils.decorators import admin_only, group_only

logger = logging.getLogger("analytics")


@Client.on_message(filters.group & ~filters.service, group=10)
async def activity_tracker(client: Client, message: Message):
    if not message.from_user or message.from_user.is_bot:
        return
    await db.add_user(message.from_user.id, message.from_user.first_name, message.from_user.username)
    is_media = bool(message.photo or message.video or message.document or message.audio)
    await db.track_activity(message.from_user.id, is_media=is_media)


@Client.on_message(filters.command("audit") & filters.group)
@admin_only
@group_only
async def audit_cmd(client: Client, message: Message):
    status = await message.reply_text("📊 Compiling audit report... this may take a moment.")

    total_members = 0
    deleted_accounts = 0
    bot_count = 0
    human_count = 0

    async for member in client.get_chat_members(message.chat.id):
        total_members += 1
        user = member.user
        if not user:
            continue
        if user.is_deleted:
            deleted_accounts += 1
        elif user.is_bot:
            bot_count += 1
        else:
            human_count += 1

    thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)
    active_cursor = db.users.find({"last_seen": {"$gte": thirty_days_ago}})
    active_ids = {doc["_id"] async for doc in active_cursor}

    all_member_ids = []
    async for member in client.get_chat_members(message.chat.id):
        if member.user and not member.user.is_bot:
            all_member_ids.append(member.user.id)

    active_count = sum(1 for uid in all_member_ids if uid in active_ids)
    inactive_count = max(0, len(all_member_ids) - active_count)

    media_pipeline = [
        {"$match": {"_id": {"$in": all_member_ids}}},
        {"$group": {"_id": None, "total_media": {"$sum": "$media_count"}}},
    ]
    media_agg = await db.users.aggregate(media_pipeline).to_list(length=1)
    total_media = media_agg[0]["total_media"] if media_agg else 0

    dup_count = await db.file_hashes.count_documents({"chat_id": message.chat.id})

    report = (
        f"📊 **Audit Report — {message.chat.title}**\n\n"
        f"👥 **Members**\n"
        f"• Total: `{total_members}`\n"
        f"• Active (30d): `{active_count}`\n"
        f"• Inactive (30d): `{inactive_count}`\n"
        f"• Deleted Accounts: `{deleted_accounts}`\n\n"
        f"🤖 **Composition**\n"
        f"• Humans: `{human_count}`\n"
        f"• Bots: `{bot_count}`\n"
        f"• Bot-to-Human Ratio: `{(bot_count / human_count):.3f}`" if human_count else "N/A"
    )
    report += (
        f"\n\n📁 **Media**\n"
        f"• Tracked media messages: `{total_media}`\n"
        f"• Unique hashed files in this chat: `{dup_count}`\n"
    )

    await status.edit_text(report)

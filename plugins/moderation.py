"""
plugins/moderation.py
----------------------
Module A: Group Cleanliness & Moderation
  - Temporary welcomer (self-destructs after N seconds)
  - Service-message cleaner (join/leave/pin/voicechat notifications)
  - Force-Subscribe (FSub) gate with inline "I've joined" callback
  - Warn engine (3-strike -> autoban) + manual ban/mute
  - Group rules engine
  - /cleandeleted purge + anti third-party-bot shield
"""

import asyncio
import logging

from hydrogram import Client, filters
from hydrogram.types import (
    Message, ChatMemberUpdated, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from hydrogram.enums import ChatMemberStatus, ChatType
from hydrogram.errors import UserNotParticipant, RPCError

from config import Config
from database import db
from utils.decorators import admin_only, group_only, owner_only
from utils.helpers import sanitize_placeholder_text, parse_duration

logger = logging.getLogger("moderation")

# =====================================================================
# 1. TEMPORARY WELCOMER
# =====================================================================

@Client.on_message(filters.new_chat_members, group=1)
async def welcomer(client: Client, message: Message):
    chat = await db.get_chat(message.chat.id)
    cfg = chat.get("welcome_config", {})
    if not cfg.get("enabled", True):
        return

    for member in message.new_chat_members:
        if member.is_bot:
            continue  # bots get handled by the anti-spy shield below

        await db.add_user(member.id, member.first_name, member.username)

        text = sanitize_placeholder_text(
            cfg.get("text", "👋 Welcome {name} to {group_name}!"),
            name=member.mention,
            id=member.id,
            group_name=message.chat.title,
        )

        try:
            banner = cfg.get("banner") or Config.DEFAULT_WELCOME_BANNER
            if banner:
                sent = await client.send_photo(message.chat.id, banner, caption=text)
            else:
                sent = await client.send_message(message.chat.id, text)
        except Exception as e:
            logger.warning(f"Welcome banner failed, falling back to text: {e}")
            sent = await client.send_message(message.chat.id, text)

        asyncio.create_task(_self_destruct(client, sent, Config.WELCOME_DELETE_SECONDS))


async def _self_destruct(client: Client, message: Message, delay: int):
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except RPCError as e:
        logger.debug(f"Self-destruct delete failed (probably already gone): {e}")


# =====================================================================
# 2. SERVICE MESSAGE CLEANER
# =====================================================================

SERVICE_FILTER = (
    filters.new_chat_members
    | filters.left_chat_member
    | filters.pinned_message
    | filters.video_chat_started
    | filters.video_chat_ended
)


@Client.on_message(SERVICE_FILTER, group=2)
async def clean_service_messages(client: Client, message: Message):
    # Note: new_chat_members is consumed by welcomer() above (group=1) first;
    # here we simply also delete Telegram's auto-generated notification bubble.
    try:
        await message.delete()
    except RPCError as e:
        logger.debug(f"Could not delete service message: {e}")


# =====================================================================
# 3. FORCE SUBSCRIBE (FSub)
# =====================================================================

async def _is_member_of_all(client: Client, user_id: int, channels: list) -> list:
    """Returns list of channel ids the user has NOT joined."""
    missing = []
    for ch in channels:
        try:
            member = await client.get_chat_member(ch, user_id)
            if member.status in (ChatMemberStatus.BANNED, ChatMemberStatus.LEFT):
                missing.append(ch)
        except UserNotParticipant:
            missing.append(ch)
        except RPCError as e:
            logger.warning(f"FSub check failed for channel {ch}: {e}")
    return missing


@Client.on_message(filters.group & ~filters.service, group=3)
async def fsub_gate(client: Client, message: Message):
    if not message.from_user:
        return
    chat = await db.get_chat(message.chat.id)
    channels = chat.get("fsub_channels") or Config.FSUB_CHANNELS
    if not channels:
        return

    missing = await _is_member_of_all(client, message.from_user.id, channels)
    if not missing:
        return

    try:
        await message.delete()
    except RPCError:
        pass

    buttons = []
    for ch in missing:
        try:
            invite_chat = await client.get_chat(ch)
            link = invite_chat.invite_link or await client.export_chat_invite_link(ch)
            buttons.append([InlineKeyboardButton(f"➕ Join {invite_chat.title}", url=link)])
        except RPCError as e:
            logger.warning(f"Could not build invite button for {ch}: {e}")
    buttons.append([InlineKeyboardButton("✅ I've Joined", callback_data="fsub_verify")])

    await client.send_message(
        message.chat.id,
        f"👋 {message.from_user.mention}, please join our channel(s) below to chat here.",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


@Client.on_callback_query(filters.regex("^fsub_verify$"))
async def fsub_verify_cb(client: Client, cq: CallbackQuery):
    chat_id = cq.message.chat.id
    chat = await db.get_chat(chat_id)
    channels = chat.get("fsub_channels") or Config.FSUB_CHANNELS
    missing = await _is_member_of_all(client, cq.from_user.id, channels)
    if missing:
        await cq.answer("❌ You still haven't joined all required channel(s).", show_alert=True)
        return
    await cq.answer("✅ Verified! You can chat now.", show_alert=True)
    try:
        await cq.message.delete()
    except RPCError:
        pass


# =====================================================================
# 4. WARN / MODERATION ENGINE
# =====================================================================

@Client.on_message(filters.command("warn") & filters.group)
@admin_only
@group_only
async def warn_cmd(client: Client, message: Message):
    if not message.reply_to_message:
        await message.reply_text("Reply to a user's message to warn them.")
        return
    target = message.reply_to_message.from_user
    reason = " ".join(message.command[1:]) or "No reason given"
    chat = await db.get_chat(message.chat.id)
    limit = chat.get("warn_limit", 3)

    count, hit_limit = await db.add_warn(message.chat.id, target.id, reason, limit)

    if hit_limit:
        try:
            await client.ban_chat_member(message.chat.id, target.id)
            await db.reset_warns(target.id)
            await message.reply_text(
                f"🔨 {target.mention} reached {limit} warns and has been banned."
            )
        except RPCError as e:
            await message.reply_text(f"Warn limit reached but ban failed: {e}")
    else:
        await message.reply_text(
            f"⚠️ {target.mention} warned ({count}/{limit}). Reason: {reason}"
        )


@Client.on_message(filters.command("unwarn") & filters.group)
@admin_only
@group_only
async def unwarn_cmd(client: Client, message: Message):
    if not message.reply_to_message:
        await message.reply_text("Reply to a user's message to unwarn them.")
        return
    target = message.reply_to_message.from_user
    await db.reset_warns(target.id)
    await message.reply_text(f"✅ Warns cleared for {target.mention}.")


@Client.on_message(filters.command("warns") & filters.group)
async def warns_cmd(client: Client, message: Message):
    target = message.reply_to_message.from_user if message.reply_to_message else message.from_user
    user = await db.get_user(target.id)
    count = user.get("warn_count", 0) if user else 0
    await message.reply_text(f"{target.mention} has {count} warning(s).")


@Client.on_message(filters.command("ban") & filters.group)
@admin_only
@group_only
async def ban_cmd(client: Client, message: Message):
    if not message.reply_to_message:
        await message.reply_text("Reply to a user's message to ban them.")
        return
    target = message.reply_to_message.from_user
    try:
        await client.ban_chat_member(message.chat.id, target.id)
        await message.reply_text(f"🔨 Banned {target.mention}.")
    except RPCError as e:
        await message.reply_text(f"Failed to ban: {e}")


@Client.on_message(filters.command("unban") & filters.group)
@admin_only
@group_only
async def unban_cmd(client: Client, message: Message):
    if len(message.command) < 2 and not message.reply_to_message:
        await message.reply_text("Usage: /unban <user_id> (or reply to their message)")
        return
    target_id = (
        message.reply_to_message.from_user.id
        if message.reply_to_message else int(message.command[1])
    )
    try:
        await client.unban_chat_member(message.chat.id, target_id)
        await message.reply_text("✅ User unbanned.")
    except RPCError as e:
        await message.reply_text(f"Failed to unban: {e}")


@Client.on_message(filters.command("mute") & filters.group)
@admin_only
@group_only
async def mute_cmd(client: Client, message: Message):
    if not message.reply_to_message:
        await message.reply_text("Reply to a user's message to mute them.")
        return
    target = message.reply_to_message.from_user
    duration = None
    if len(message.command) > 1:
        duration = parse_duration(message.command[1])

    from hydrogram.types import ChatPermissions
    until_date = None
    if duration:
        import time
        until_date = int(time.time() + duration.total_seconds())

    try:
        await client.restrict_chat_member(
            message.chat.id, target.id, ChatPermissions(), until_date=until_date
        )
        suffix = f" for {message.command[1]}" if duration else " indefinitely"
        await message.reply_text(f"🔇 Muted {target.mention}{suffix}.")
    except RPCError as e:
        await message.reply_text(f"Failed to mute: {e}")


@Client.on_message(filters.command("unmute") & filters.group)
@admin_only
@group_only
async def unmute_cmd(client: Client, message: Message):
    if not message.reply_to_message:
        await message.reply_text("Reply to a user's message to unmute them.")
        return
    target = message.reply_to_message.from_user
    from hydrogram.types import ChatPermissions
    try:
        await client.restrict_chat_member(
            message.chat.id, target.id,
            ChatPermissions(
                can_send_messages=True, can_send_media_messages=True,
                can_send_other_messages=True, can_add_web_page_previews=True,
            ),
        )
        await message.reply_text(f"🔊 Unmuted {target.mention}.")
    except RPCError as e:
        await message.reply_text(f"Failed to unmute: {e}")


# =====================================================================
# GROUP RULES ENGINE
# =====================================================================

@Client.on_message(filters.command("setrules") & filters.group)
@admin_only
@group_only
async def setrules_cmd(client: Client, message: Message):
    rules_text = message.text.split(None, 1)
    if len(rules_text) < 2:
        await message.reply_text("Usage: /setrules <text> (markdown supported)")
        return
    await db.update_chat(message.chat.id, {"rules": rules_text[1]})
    await message.reply_text("✅ Rules updated.")


@Client.on_message(filters.command("rules") & filters.group)
async def rules_cmd(client: Client, message: Message):
    chat = await db.get_chat(message.chat.id)
    rules = chat.get("rules")
    if not rules:
        await message.reply_text("No rules have been set for this group yet.")
        return
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("📜 Full Rules", callback_data="show_rules")]]
    )
    await message.reply_text("Tap below to view the group rules.", reply_markup=kb)


@Client.on_callback_query(filters.regex("^show_rules$"))
async def show_rules_cb(client: Client, cq: CallbackQuery):
    chat = await db.get_chat(cq.message.chat.id)
    rules = chat.get("rules") or "No rules set."
    await cq.answer()
    await client.send_message(cq.message.chat.id, f"📜 **Group Rules**\n\n{rules}")


# =====================================================================
# 5. DELETED-ACCOUNT PURGE
# =====================================================================

@Client.on_message(filters.command("cleandeleted") & filters.group)
@admin_only
@group_only
async def clean_deleted_cmd(client: Client, message: Message):
    status_msg = await message.reply_text("🔍 Scanning members for deleted accounts...")
    removed = 0
    scanned = 0
    async for member in client.get_chat_members(message.chat.id):
        scanned += 1
        user = member.user
        if user and user.is_deleted:
            try:
                await client.ban_chat_member(message.chat.id, user.id)
                await client.unban_chat_member(message.chat.id, user.id)  # ban then unban = kick
                removed += 1
            except RPCError as e:
                logger.warning(f"Could not remove deleted account {user.id}: {e}")
        if scanned % 200 == 0:
            await status_msg.edit_text(f"🔍 Scanned {scanned} members, removed {removed}...")

    await status_msg.edit_text(
        f"✅ Scan complete. Scanned {scanned} members, removed {removed} deleted accounts."
    )


# =====================================================================
# ANTI-SPY / UNAUTHORIZED BOT SHIELD
# =====================================================================

@Client.on_chat_member_updated(filters.group)
async def anti_spy_shield(client: Client, event: ChatMemberUpdated):
    new_member = event.new_chat_member
    if not new_member or not new_member.user.is_bot:
        return
    if new_member.status not in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR):
        return

    added_by = event.from_user
    bot_id = new_member.user.id

    # Allow the bot itself, and bots explicitly added by the owner/sudo.
    from config import Config as C
    if bot_id == client.me.id:
        return
    if added_by and (added_by.id == C.OWNER_ID or added_by.id in C.SUDO_USERS):
        return

    try:
        await client.ban_chat_member(event.chat.id, bot_id)
        await client.unban_chat_member(event.chat.id, bot_id)
        await client.send_message(
            event.chat.id,
            f"🛡️ Unauthorized bot `@{new_member.user.username}` was auto-removed. "
            f"Only the owner can add bots to this group.",
        )
    except RPCError as e:
        logger.warning(f"Anti-spy shield failed to remove bot {bot_id}: {e}")


# =====================================================================
# WELCOME CONFIG COMMANDS
# =====================================================================

@Client.on_message(filters.command("setwelcome") & filters.group)
@admin_only
@group_only
async def set_welcome_cmd(client: Client, message: Message):
    parts = message.text.split(None, 1)
    if len(parts) < 2:
        await message.reply_text(
            "Usage: /setwelcome <text>\nPlaceholders: {name} {id} {group_name}"
        )
        return
    chat = await db.get_chat(message.chat.id)
    cfg = chat.get("welcome_config", {})
    cfg["text"] = parts[1]
    banner = None
    if message.reply_to_message and message.reply_to_message.photo:
        banner = message.reply_to_message.photo.file_id
        cfg["banner"] = banner
    await db.update_chat(message.chat.id, {"welcome_config": cfg})
    await message.reply_text("✅ Welcome message updated." + (" Banner set." if banner else ""))


@Client.on_message(filters.command("welcometoggle") & filters.group)
@admin_only
@group_only
async def welcome_toggle_cmd(client: Client, message: Message):
    chat = await db.get_chat(message.chat.id)
    cfg = chat.get("welcome_config", {})
    cfg["enabled"] = not cfg.get("enabled", True)
    await db.update_chat(message.chat.id, {"welcome_config": cfg})
    await message.reply_text(f"Welcomer is now {'ON ✅' if cfg['enabled'] else 'OFF ❌'}.")

"""
plugins/vip.py
---------------
Module C: Automated VIP Subscription & UPI Billing
  - /vip paywall workflow with UPI QR + VPA
  - Admin [Approve]/[Reject] callback -> PIL-rendered invoice DM'd to user
  - Hourly APScheduler cron: expiry reminders (48h/24h) + auto-kick on expiry
"""

import logging
import os
import uuid
from datetime import datetime, timedelta, timezone

from hydrogram import Client, filters
from hydrogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
)
from hydrogram.errors import RPCError

from config import Config
from database import db
from utils.decorators import admin_only, owner_only
from utils.helpers import plan_to_timedelta, human_dt
from utils.invoice import generate_invoice

logger = logging.getLogger("vip")

PLANS = {
    "1month": {"label": "1 Month", "price": "₹99"},
    "3months": {"label": "3 Months", "price": "₹249"},
    "lifetime": {"label": "Lifetime", "price": "₹999"},
}


# =====================================================================
# ADMIN: set/update pricing plans (stored in chats collection at bot-level "0")
# =====================================================================

@Client.on_message(filters.command("setprice"))
@owner_only
async def set_price_cmd(client: Client, message: Message):
    """Usage: /setprice <plan_key> <price>  e.g. /setprice 1month ₹149"""
    if len(message.command) < 3:
        await message.reply_text("Usage: /setprice <1month|3months|lifetime> <price>")
        return
    key, price = message.command[1], message.command[2]
    if key not in PLANS:
        await message.reply_text(f"Unknown plan. Choose from: {', '.join(PLANS)}")
        return
    PLANS[key]["price"] = price
    await message.reply_text(f"✅ {PLANS[key]['label']} price updated to {price}.")


# =====================================================================
# USER: request VIP
# =====================================================================

@Client.on_message(filters.command("vip") & filters.private)
async def vip_cmd(client: Client, message: Message):
    buttons = [
        [InlineKeyboardButton(f"{p['label']} — {p['price']}", callback_data=f"vip_plan_{key}")]
        for key, p in PLANS.items()
    ]
    await message.reply_text(
        "💎 **VIP Membership Plans**\n\nChoose a plan to continue:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


@Client.on_callback_query(filters.regex(r"^vip_plan_(\w+)$"))
async def vip_plan_cb(client: Client, cq: CallbackQuery):
    plan_key = cq.matches[0].group(1)
    plan = PLANS.get(plan_key)
    if not plan:
        await cq.answer("Invalid plan.", show_alert=True)
        return

    await cq.answer()
    text = (
        f"💳 **{plan['label']} — {plan['price']}**\n\n"
        f"Pay via UPI to: `{Config.UPI_VPA}`\n\n"
        f"After payment, send the **Transaction ID** or a **screenshot** of the payment "
        f"here as your next message, prefixed with:\n`/paid {plan_key}`"
    )
    if Config.UPI_QR_FILE_ID:
        await client.send_photo(cq.message.chat.id, Config.UPI_QR_FILE_ID, caption=text)
    else:
        await client.send_message(cq.message.chat.id, text)


@Client.on_message(filters.command("paid") & filters.private)
async def paid_cmd(client: Client, message: Message):
    if len(message.command) < 2 or message.command[1] not in PLANS:
        await message.reply_text("Usage: /paid <1month|3months|lifetime> (as a reply to your "
                                  "payment screenshot, or followed by the transaction ID)")
        return
    plan_key = message.command[1]
    plan = PLANS[plan_key]

    tx_id = uuid.uuid4().hex[:10].upper()
    screenshot_id = None
    if message.reply_to_message and message.reply_to_message.photo:
        screenshot_id = message.reply_to_message.photo.file_id

    await db.create_transaction(
        tx_id, message.from_user.id, message.chat.id, plan["price"], plan_key, screenshot_id
    )

    admin_text = (
        f"🆕 **VIP Payment Verification Needed**\n\n"
        f"User: {message.from_user.mention} (`{message.from_user.id}`)\n"
        f"Plan: {plan['label']} — {plan['price']}\n"
        f"Tx ID: `{tx_id}`"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"vip_approve_{tx_id}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"vip_reject_{tx_id}"),
    ]])

    if screenshot_id:
        await client.send_photo(Config.OWNER_ID, screenshot_id, caption=admin_text, reply_markup=kb)
    else:
        await client.send_message(Config.OWNER_ID, admin_text, reply_markup=kb)

    await message.reply_text("✅ Your payment is submitted for verification. You'll be notified shortly.")


# =====================================================================
# ADMIN: approve / reject
# =====================================================================

@Client.on_callback_query(filters.regex(r"^vip_(approve|reject)_(\w+)$"))
@admin_only
async def vip_decision_cb(client: Client, cq: CallbackQuery):
    action, tx_id = cq.matches[0].group(1), cq.matches[0].group(2)
    tx = await db.transactions.find_one({"_id": tx_id})
    if not tx:
        await cq.answer("Transaction not found.", show_alert=True)
        return
    if tx["status"] != "pending":
        await cq.answer(f"Already {tx['status']}.", show_alert=True)
        return

    user_id = tx["user_id"]
    plan_key = tx["plan"]
    plan = PLANS.get(plan_key, {"label": plan_key, "price": tx["amount"]})

    if action == "reject":
        await db.set_transaction_status(tx_id, "rejected")
        await cq.answer("Rejected.")
        await cq.message.edit_caption(cq.message.caption + "\n\n❌ REJECTED") if cq.message.caption \
            else await cq.message.edit_text(cq.message.text + "\n\n❌ REJECTED")
        try:
            await client.send_message(user_id, "❌ Your VIP payment could not be verified. "
                                                 "Please contact support.")
        except RPCError:
            pass
        return

    # ---- approve ----
    expiry = datetime.now(timezone.utc) + plan_to_timedelta(plan_key)
    await db.set_vip(user_id, plan_key, expiry)

    invite_link = None
    if Config.VIP_GROUP_ID:
        try:
            invite = await client.create_chat_invite_link(
                Config.VIP_GROUP_ID, member_limit=1,
                expire_date=int((datetime.now(timezone.utc) + timedelta(days=1)).timestamp()),
            )
            invite_link = invite.invite_link
        except RPCError as e:
            logger.warning(f"Could not create VIP invite link: {e}")
            invite_link = "Contact admin for group access."

    user = await client.get_users(user_id)
    invoice_id = f"INV-{tx_id}"
    out_path = os.path.join(Config.TMP_DOWNLOAD_DIR, f"invoice_{tx_id}.png")
    generate_invoice(
        member_name=user.first_name,
        member_id=user_id,
        plan_name=plan["label"],
        price_paid=str(plan["price"]),
        invite_link=invite_link or "N/A",
        expiry_date=expiry,
        invoice_id=invoice_id,
        out_path=out_path,
    )

    await db.set_transaction_status(tx_id, "approved", invoice_id)

    try:
        await client.send_photo(
            user_id, out_path,
            caption=f"🎉 Welcome to VIP! Your **{plan['label']}** plan is active until "
                    f"{human_dt(expiry)}.",
        )
    except RPCError as e:
        logger.warning(f"Could not DM invoice to {user_id}: {e}")

    await cq.answer("Approved ✅")
    edited = (cq.message.caption or cq.message.text or "") + "\n\n✅ APPROVED"
    try:
        if cq.message.caption is not None:
            await cq.message.edit_caption(edited)
        else:
            await cq.message.edit_text(edited)
    except RPCError:
        pass


# =====================================================================
# SUBSCRIPTION EXPIRY ENGINE (called by scheduler in bot.py)
# =====================================================================

async def run_expiry_check(client: Client):
    """Hourly job: send 48h/24h reminders, and revoke/kick fully-expired VIPs."""
    now = datetime.now(timezone.utc)

    # --- fully expired: revoke + kick ---
    expired = await db.expiring_vips(before=now)
    for user in expired:
        uid = user["_id"]
        await db.revoke_vip(uid)
        if Config.VIP_GROUP_ID:
            try:
                await client.ban_chat_member(Config.VIP_GROUP_ID, uid)
                await client.unban_chat_member(Config.VIP_GROUP_ID, uid)
            except RPCError as e:
                logger.warning(f"Could not remove expired VIP {uid} from group: {e}")
        try:
            await client.send_message(uid, "⌛ Your VIP subscription has expired. "
                                            "Use /vip to renew and keep your access.")
        except RPCError:
            pass
        if Config.VIP_GROUP_ID:
            try:
                await client.send_message(Config.VIP_GROUP_ID,
                                           f"A member's VIP subscription has expired and access was revoked.")
            except RPCError:
                pass

    # --- 48h / 24h reminders ---
    for hours, flag in ((48, "reminded_48"), (24, "reminded_24")):
        window_start = now
        window_end = now + timedelta(hours=hours)
        cursor = db.users.find({
            "is_vip": True,
            "vip_expiry": {"$gte": window_start, "$lte": window_end},
            flag: {"$ne": True},
        })
        async for user in cursor:
            uid = user["_id"]
            try:
                await client.send_message(
                    uid, f"⏰ Reminder: your VIP subscription expires in ~{hours} hours "
                         f"({human_dt(user['vip_expiry'])}). Use /vip to renew."
                )
            except RPCError:
                pass
            if Config.VIP_GROUP_ID:
                try:
                    await client.send_message(
                        Config.VIP_GROUP_ID,
                        f"⏰ A member's VIP access expires in ~{hours} hours."
                    )
                except RPCError:
                    pass
            await db.users.update_one({"_id": uid}, {"$set": {flag: True}})

    logger.info(f"Expiry check complete: {len(expired)} revoked.")

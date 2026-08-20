"""
bot.py
------
Main entry point.

Responsibilities:
  - Initialise the Hydrogram Client with plugin auto-loading.
  - Verify MongoDB connectivity and build indexes before serving traffic.
  - Start the AsyncIOScheduler for cron-style jobs (VIP expiry engine,
    scheduled broadcasts).
  - Graceful shutdown handling.
"""

import asyncio
import logging
import sys
from datetime import datetime, timezone

try:
    import uvloop
    uvloop.install()
except ImportError:
    pass  # uvloop is optional / not available on Windows

from hydrogram import Client
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

from config import Config
from database import db

logger = logging.getLogger("bot")

app = Client(
    name="superbot",
    api_id=Config.API_ID,
    api_hash=Config.API_HASH,
    bot_token=Config.BOT_TOKEN,
    workers=Config.WORKERS,
    plugins=dict(root="plugins"),
)

scheduler = AsyncIOScheduler(timezone="UTC")


async def _vip_expiry_job():
    from plugins.vip import run_expiry_check
    try:
        await run_expiry_check(app)
    except Exception:
        logger.exception("VIP expiry job failed")


def schedule_broadcast(run_at: datetime, source_message, body: str, keyboard):
    """
    Called from plugins/broadcast.py's /broadcast_at handler to register a
    one-off future broadcast job with APScheduler.
    """
    from plugins.broadcast import _do_broadcast

    async def _job():
        status = await source_message.reply_text("📢 Running scheduled broadcast...")
        await _do_broadcast(app, source_message, body, keyboard, status)

    scheduler.add_job(_job, trigger=DateTrigger(run_date=run_at))


async def main():
    logger.info("Connecting to MongoDB...")
    if not await db.ping():
        logger.critical("Could not connect to MongoDB. Check MONGO_URI. Exiting.")
        sys.exit(1)
    await db.ensure_indexes()

    logger.info("Starting Telegram client...")
    await app.start()
    me = await app.get_me()
    logger.info(f"Logged in as @{me.username} (id={me.id})")

    scheduler.add_job(
        _vip_expiry_job,
        "interval",
        minutes=Config.SUBSCRIPTION_CHECK_INTERVAL_MIN,
        next_run_time=datetime.now(timezone.utc),  # run once immediately on boot
        id="vip_expiry_check",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Scheduler started (VIP expiry engine active).")

    logger.info("Bot is up and running. Press Ctrl+C to stop.")
    await asyncio.Event().wait()  # run forever


async def shutdown():
    logger.info("Shutting down...")
    scheduler.shutdown(wait=False)
    await app.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        asyncio.run(shutdown())

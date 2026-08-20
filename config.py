"""
config.py
---------
Centralised, fail-fast configuration loader.

All secrets/config come from environment variables (12-factor style),
optionally loaded from a local `.env` file via python-dotenv for local dev.
On a VPS these should be set in the systemd unit / docker-compose env block
instead of committing a .env file.
"""

import os
import sys
import logging
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("config")


def _get_env(name: str, default=None, required: bool = False, cast=str):
    val = os.environ.get(name, default)
    if required and (val is None or val == ""):
        logger.critical(f"Missing required environment variable: {name}")
        sys.exit(1)
    if val is None:
        return None
    try:
        if cast is bool:
            return str(val).strip().lower() in ("1", "true", "yes", "on")
        if cast is list:
            return [x.strip() for x in str(val).split(",") if x.strip()]
        return cast(val)
    except (ValueError, TypeError):
        logger.critical(f"Env var {name} could not be cast to {cast}")
        sys.exit(1)


class Config:
    # ---- Telegram core credentials (my.telegram.org) ----
    API_ID: int = _get_env("API_ID", required=True, cast=int)
    API_HASH: str = _get_env("API_HASH", required=True, cast=str)
    BOT_TOKEN: str = _get_env("BOT_TOKEN", required=True, cast=str)

    # ---- Ownership / access control ----
    OWNER_ID: int = _get_env("OWNER_ID", required=True, cast=int)
    SUDO_USERS: list = _get_env("SUDO_USERS", default="", cast=list)  # comma separated ids
    SUDO_USERS = [int(x) for x in SUDO_USERS] if SUDO_USERS else []

    # ---- MongoDB ----
    MONGO_URI: str = _get_env("MONGO_URI", required=True, cast=str)
    MONGO_DB_NAME: str = _get_env("MONGO_DB_NAME", default="superbot", cast=str)

    # ---- FileStore / private storage channel ----
    DB_CHANNEL_ID: int = _get_env("DB_CHANNEL_ID", required=True, cast=int)

    # ---- Force Subscribe ----
    FSUB_CHANNELS: list = _get_env("FSUB_CHANNELS", default="", cast=list)
    FSUB_CHANNELS = [int(x) for x in FSUB_CHANNELS] if FSUB_CHANNELS else []

    # ---- VIP / Billing ----
    UPI_VPA: str = _get_env("UPI_VPA", default="", cast=str)
    UPI_QR_FILE_ID: str = _get_env("UPI_QR_FILE_ID", default="", cast=str)
    VIP_GROUP_ID: int = _get_env("VIP_GROUP_ID", default=0, cast=int)

    # ---- Welcome / media assets ----
    WELCOME_DELETE_SECONDS: int = _get_env("WELCOME_DELETE_SECONDS", default=10, cast=int)
    DEFAULT_WELCOME_BANNER: str = _get_env(
        "DEFAULT_WELCOME_BANNER", default="assets/welcome_default.jpg", cast=str
    )
    INVOICE_TEMPLATE_PATH: str = _get_env(
        "INVOICE_TEMPLATE_PATH", default="assets/invoice_template.png", cast=str
    )
    FONT_REGULAR_PATH: str = _get_env(
        "FONT_REGULAR_PATH", default="assets/fonts/Regular.ttf", cast=str
    )
    FONT_BOLD_PATH: str = _get_env(
        "FONT_BOLD_PATH", default="assets/fonts/Bold.ttf", cast=str
    )

    # ---- Scheduler / duplicate-hash tuning ----
    SUBSCRIPTION_CHECK_INTERVAL_MIN: int = _get_env(
        "SUBSCRIPTION_CHECK_INTERVAL_MIN", default=60, cast=int
    )
    PHASH_DUPLICATE_THRESHOLD: int = _get_env(
        "PHASH_DUPLICATE_THRESHOLD", default=6, cast=int
    )

    # ---- Runtime ----
    WORKERS: int = _get_env("WORKERS", default=8, cast=int)
    LOG_LEVEL: str = _get_env("LOG_LEVEL", default="INFO", cast=str)
    TMP_DOWNLOAD_DIR: str = _get_env("TMP_DOWNLOAD_DIR", default="downloads", cast=str)


os.makedirs(Config.TMP_DOWNLOAD_DIR, exist_ok=True)

logging.basicConfig(
    level=getattr(logging, Config.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

"""
Configuration management - loads from .env and exposes typed settings.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# -- Telegram API --
API_ID: int = int(os.getenv("API_ID", "0"))
API_HASH: str = os.getenv("API_HASH", "")
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
PHONE: str = os.getenv("PHONE", "")
USER_STRING_SESSION: str = os.getenv("USER_STRING_SESSION", "")

# -- Session names --
USER_SESSION: str = os.getenv("USER_SESSION", "user_session")
BOT_SESSION: str = os.getenv("BOT_SESSION", "bot_session")

# -- Admins --
ADMIN_IDS: set[int] = {
    int(uid.strip())
    for uid in os.getenv("ADMIN_IDS", "").split(",")
    if uid.strip().isdigit()
}

# -- Database --
DB_PATH: str = os.getenv("DB_PATH", "data/bot.db")


def validate() -> None:
    """Raise if any required config is missing."""
    missing = []
    if not API_ID:
        missing.append("API_ID")
    if not API_HASH:
        missing.append("API_HASH")
    if not BOT_TOKEN:
        missing.append("BOT_TOKEN")
    if not PHONE:
        missing.append("PHONE")
    if not ADMIN_IDS:
        missing.append("ADMIN_IDS")
    if missing:
        raise SystemExit(
            f"[FATAL] Missing required config: {', '.join(missing)}\n"
            "Copy .env.example to .env and fill in your credentials."
        )

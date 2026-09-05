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

# -- Forwarding behavior --
# When enabled, messages posted by the bot's own user account in a source
# chat are forwarded too. Off by default to avoid accidental loops.
FORWARD_OWN_MESSAGES: bool = (
    os.getenv("FORWARD_OWN_MESSAGES", "").strip().lower()
    in {"1", "true", "yes", "on"}
)

# Raw update logging is useful while diagnosing Telegram delivery, but writing
# one log line for every channel update can itself delay the update loop during
# a backlog. Keep it opt-in for production.
LOG_RAW_UPDATES: bool = (
    os.getenv("LOG_RAW_UPDATES", "").strip().lower()
    in {"1", "true", "yes", "on"}
)

# Telegram can occasionally delay channel push updates. Poll mapped sources
# as a safety net so forwarding does not depend only on push delivery.
SOURCE_POLL_INTERVAL_SECONDS: float = float(
    os.getenv("SOURCE_POLL_INTERVAL_SECONDS", "5")
)


def validate() -> None:
    """Raise if any required config is missing."""
    missing = []
    if not API_ID:
        missing.append("API_ID")
    if not API_HASH:
        missing.append("API_HASH")
    if not BOT_TOKEN:
        missing.append("BOT_TOKEN")
    # A Railway deployment normally authenticates with USER_STRING_SESSION;
    # PHONE is only needed when starting an interactive local session.
    if not PHONE and not USER_STRING_SESSION:
        missing.append("PHONE")
    if not ADMIN_IDS:
        missing.append("ADMIN_IDS")
    if missing:
        raise SystemExit(
            f"[FATAL] Missing required config: {', '.join(missing)}\n"
            "Copy .env.example to .env and fill in your credentials."
        )

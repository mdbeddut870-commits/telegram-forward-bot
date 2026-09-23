"""
Configuration management - loads from .env and exposes typed settings.
"""

import os
from dotenv import load_dotenv

load_dotenv()


def _get_int(name: str, default: int) -> int:
    """Read an integer env var, falling back to default on bad input."""
    raw = os.getenv(name, "")
    try:
        return int(raw) if raw.strip() else default
    except (ValueError, AttributeError):
        return default


def _get_float(name: str, default: float) -> float:
    """Read a float env var, falling back to default on bad input."""
    raw = os.getenv(name, "")
    try:
        return float(raw) if raw.strip() else default
    except (ValueError, AttributeError):
        return default


def _get_bool(name: str, default: bool) -> bool:
    """Read a boolean env var ('1', 'true', 'yes', 'on' are truthy)."""
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _get_int_set(name: str) -> set[int]:
    """Read a comma-separated int set, skipping bad entries."""
    result: set[int] = set()
    for value in os.getenv(name, "").split(","):
        value = value.strip()
        if not value:
            continue
        try:
            result.add(int(value))
        except ValueError:
            continue
    return result

# -- Telegram API --
API_ID: int = _get_int("API_ID", 0)
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

# -- Web dashboard --
# Shared secret for the mini dashboard served by the health server.
# Empty value disables the dashboard (403 setup hint).
DASHBOARD_TOKEN: str = os.getenv("DASHBOARD_TOKEN", "")

# -- Forwarding behavior --
# When enabled, messages posted by the bot's own user account in a source
# chat are forwarded too. Off by default to avoid accidental loops.
FORWARD_OWN_MESSAGES: bool = _get_bool("FORWARD_OWN_MESSAGES", False)

# Raw update logging is useful while diagnosing Telegram delivery, but writing
# one log line for every channel update can itself delay the update loop during
# a backlog. Keep it opt-in for production.
LOG_RAW_UPDATES: bool = _get_bool("LOG_RAW_UPDATES", False)

# Prioritize new posts over replaying an offline backlog.
CATCH_UP: bool = _get_bool("CATCH_UP", False)

# Concurrent sends reduce delay across multiple destinations.
# 16 is the sweet spot: enough parallelism for 30 sources, low enough to
# avoid ForwardMessagesRequest flood-waits from burst traffic.
SEND_CONCURRENCY: int = max(1, _get_int("SEND_CONCURRENCY", 16))

# History polling covers Telegram channel push-update delays.  Polling every
# few seconds is safe when each request is small and concurrent history
# fetches are capped; a large sequential scan starves live updates.
SOURCE_POLL_INTERVAL_SECONDS: float = max(
    1.0, _get_float("SOURCE_POLL_INTERVAL_SECONDS", 10)
)
# Cap concurrent GetHistory calls so 30 sources polled every few seconds do
# not trigger Telegram flood waits in bursts.  12 keeps a 30-source cycle
# around 5-6s; at 6 the cycle took ~13s and delayed detection.
SOURCE_POLL_CONCURRENCY: int = max(1, _get_int("SOURCE_POLL_CONCURRENCY", 12))
SOURCE_POLL_HISTORY_LIMIT: int = max(1, _get_int("SOURCE_POLL_HISTORY_LIMIT", 5))
SOURCE_POLL_SOURCE_IDS: set[int] = _get_int_set("SOURCE_POLL_SOURCE_IDS")

SOURCE_POLL_ALL_MAPPED: bool = _get_bool("SOURCE_POLL_ALL_MAPPED", False)

# Optional public usernames resolved automatically on startup.
AUTO_SOURCE_USERNAMES: list[str] = [
    value.strip().lstrip("@")
    for value in os.getenv("AUTO_SOURCE_USERNAMES", "").split(",")
    if value.strip()
]
AUTO_SOURCE_DEST_ID: int = _get_int("AUTO_SOURCE_DEST_ID", 0)
REMOVE_SOURCE_IDS: set[int] = _get_int_set("REMOVE_SOURCE_IDS")

# -- KuCoin promo footer --
# Appended to forwarded copies whose original text mentions KuCoin.
# Override via env without a code change if the referral link rotates.
KUCOIN_REGISTER_LINE: str = os.getenv(
    "KUCOIN_REGISTER_LINE",
    "Register Link = https://www.kucoin.com/pt/gemslot/MHA?fromHome=true&rcode=CXEEW12K&utm_source=gemslot",
)
KUCOIN_REGISTER_URL: str = os.getenv(
    "KUCOIN_REGISTER_URL",
    "https://www.kucoin.com/pt/gemslot/MHA?fromHome=true&rcode=CXEEW12K&utm_source=gemslot",
)
KUCOIN_BUTTON_TEXT: str = os.getenv("KUCOIN_BUTTON_TEXT", "🔗 Register / Join Now")
# "button" = native forward + inline URL button (blue header kept).
# "copy" = single attributed copy with register line (no second post).
KUCOIN_MODE: str = os.getenv("KUCOIN_MODE", "copy").strip().lower()
KUCOIN_KEYWORDS: list[str] = [
    value.strip().lower()
    for value in os.getenv("KUCOIN_KEYWORDS", "kucoin").split(",")
    if value.strip()
]

# -- Dedup --
# Minimum normalized text length before a post is text-dedupable.
# Shorter texts are too generic to match safely.
try:
    DEDUP_MIN_TEXT_CHARS: int = max(0, int(os.getenv("DEDUP_MIN_TEXT_CHARS", "20") or 20))
except ValueError:
    DEDUP_MIN_TEXT_CHARS = 20


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

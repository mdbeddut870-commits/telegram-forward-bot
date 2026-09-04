#!/usr/bin/env python3
"""
Telegram Auto-Forward Bot - Main entry point.

Dual-client architecture:
  - User client: listens to source channels/groups (sees ALL messages)
  - Bot client:  handles admin commands via @YourBot

Telegram bots cannot see channel posts or non-command group messages
due to privacy mode, so the user client is used for forwarding.
"""

import asyncio
import logging
import sys

from telethon import TelegramClient
from telethon.sessions import StringSession

import config
import database as db
from forwarder import register_forward_handler
from handlers import register_bot_handlers

# -- Logging setup --
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("bot")


async def _check_mapping_access(client: TelegramClient, mappings: list[dict]) -> None:
    """Resolve every configured chat with the forwarding user account.

    A mapping can exist in SQLite even when the logged-in Telegram account is
    no longer a member of the source or cannot post to the destination.  This
    startup check makes that common failure visible immediately instead of
    looking like a silent forwarding failure.
    """
    chat_ids = sorted({
        chat_id
        for mapping in mappings
        if mapping["active"]
        for chat_id in (mapping["source_id"], mapping["dest_id"])
    })
    for chat_id in chat_ids:
        try:
            entity = await client.get_entity(chat_id)
            title = getattr(entity, "title", None) or getattr(entity, "first_name", None) or str(chat_id)
            logger.info("Mapping chat accessible: %s (%s)", chat_id, title)
        except Exception as exc:
            logger.error(
                "Mapping chat access FAILED: %s | %s: %s",
                chat_id,
                type(exc).__name__,
                exc,
            )


async def main() -> None:
    config.validate()

    db.init_db()
    logger.info("Database initialised at %s", config.DB_PATH)
    mappings = db.list_mappings()
    active_mappings = [mapping for mapping in mappings if mapping["active"]]
    logger.info(
        "Loaded %d forwarding mappings (%d active) | sources=%s",
        len(mappings),
        len(active_mappings),
        sorted({mapping["source_id"] for mapping in active_mappings}),
    )

    # -- User client (receives messages from source chats) --
    # Railway uses a StringSession secret because the local .session file is
    # intentionally excluded from deployment. Local development keeps using
    # the normal file session when USER_STRING_SESSION is not configured.
    user_session = (
        StringSession(config.USER_STRING_SESSION)
        if config.USER_STRING_SESSION
        else config.USER_SESSION
    )
    user_client = TelegramClient(user_session, config.API_ID, config.API_HASH)
    if config.USER_STRING_SESSION:
        await user_client.connect()
        if not await user_client.is_user_authorized():
            raise RuntimeError("USER_STRING_SESSION is not authorized")
    else:
        await user_client.start(phone=config.PHONE)
    me = await user_client.get_me()
    logger.info("User client logged in as %s (%d)", me.first_name, me.id)
    # Refresh the account's dialogs after reconnecting.  This makes Telegram
    # re-synchronise channel subscriptions, which is important for channels
    # joined while the StringSession was created or while the service was down.
    dialogs = await user_client.get_dialogs()
    logger.info("Telegram dialog sync complete: %d dialogs", len(dialogs))
    await _check_mapping_access(user_client, active_mappings)

    # -- Bot client (handles admin commands) --
    bot_client = TelegramClient(
        config.BOT_SESSION,
        config.API_ID,
        config.API_HASH,
    )
    await bot_client.start(bot_token=config.BOT_TOKEN)
    logger.info("Bot client started with token")

    # Store user id so forwarder can skip own messages
    user_client._me_id = me.id

    # -- Register handlers --
    register_bot_handlers(bot_client)
    register_forward_handler(user_client)

    logger.info("Both clients running. Press Ctrl+C to stop.")

    # -- Run both clients concurrently --
    await asyncio.gather(
        user_client.run_until_disconnected(),
        bot_client.run_until_disconnected(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
    except Exception as exc:
        logger.exception("Fatal error: %s", exc)
        sys.exit(1)

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


async def main() -> None:
    config.validate()

    db.init_db()
    logger.info("Database initialised at %s", config.DB_PATH)

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

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
import threading
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from telethon import TelegramClient
from telethon.sessions import StringSession

import config
import database as db
from forwarder import (
    register_forward_handler,
    shutdown_forwarding_tasks,
    start_source_polling,
)
from handlers import register_bot_handlers

# -- Logging setup --
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("bot")


class _HealthHandler(BaseHTTPRequestHandler):
    """Minimal HTTP endpoint required by the Cloudflare Container probe."""

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        if self.path not in ("/ping", "/health"):
            self.send_response(404)
            self.end_headers()
            return
        body = b'{"status":"running"}\n'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        # Keep the bot logs focused on Telegram events.
        return


def _start_health_server() -> ThreadingHTTPServer:
    """Start the container readiness/liveness server in a daemon thread."""
    server = ThreadingHTTPServer(("0.0.0.0", 8080), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, name="health-server", daemon=True)
    thread.start()
    logger.info("Health server listening on port 8080")
    return server


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


async def _add_configured_sources(client: TelegramClient) -> None:
    """Resolve public usernames and persist source->destination mappings."""
    if not config.AUTO_SOURCE_USERNAMES or not config.AUTO_SOURCE_DEST_ID:
        return
    added = 0
    for username in config.AUTO_SOURCE_USERNAMES:
        try:
            entity = await client.get_entity(username)
            source_id = int(entity.id)
            if getattr(entity, "broadcast", False) or getattr(entity, "megagroup", False):
                source_id = int(f"-100{source_id}")
            row_id = db.add_mapping(source_id, config.AUTO_SOURCE_DEST_ID, getattr(entity, "title", None) or username, str(config.AUTO_SOURCE_DEST_ID))
            if row_id:
                added += 1
                logger.info("Auto-added source mapping: @%s -> %s", username, config.AUTO_SOURCE_DEST_ID)
        except Exception:
            logger.exception("Could not resolve auto source @%s", username)
    logger.info("Auto-source setup complete: %d new mapping(s)", added)


async def _remove_configured_sources(client: TelegramClient) -> None:
    """Resolve usernames and remove all their forwarding mappings."""
    removed = 0
    for username in config.REMOVE_SOURCE_USERNAMES:
        try:
            entity = await client.get_entity(username)
            source_id = int(entity.id)
            if getattr(entity, "broadcast", False) or getattr(entity, "megagroup", False):
                source_id = int(f"-100{source_id}")
            count = db.remove_mappings_for_source(source_id)
            removed += count
            logger.info("Removed %d mapping(s) for source @%s", count, username)
        except Exception:
            logger.exception("Could not remove source @%s", username)
    logger.info("Source removal complete: %d mapping(s) removed", removed)


async def _run_client_forever(client: TelegramClient, name: str) -> None:
    """Keep a Telegram client running after a transient disconnect.

    Telethon normally reconnects transport-level failures itself, but
    ``run_until_disconnected`` returns when a client is fully disconnected.
    Without this supervisor the task would finish and the client would never
    receive new updates again while the Railway process still looked healthy.
    """
    while True:
        await client.run_until_disconnected()
        logger.warning("%s client disconnected; reconnecting in 5 seconds", name)
        await asyncio.sleep(5)
        try:
            await client.connect()
            if not await client.is_user_authorized() if name == "User" else False:
                raise RuntimeError(f"{name} client is no longer authorized")
            logger.info("%s client reconnected", name)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Failed to reconnect %s client; retrying in 10 seconds", name)
            await asyncio.sleep(10)


async def main() -> None:
    health_server = _start_health_server()
    try:
        await _run_bot()
    finally:
        await shutdown_forwarding_tasks()
        health_server.shutdown()


async def _run_bot() -> None:
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
    # Ask Telethon to recover updates that arrived while the session was
    # reconnecting instead of silently starting from the newest update.
    user_client = TelegramClient(
        user_session,
        config.API_ID,
        config.API_HASH,
        # Prioritize live posts over replaying an offline backlog.
        catch_up=config.CATCH_UP,
    )
    if config.USER_STRING_SESSION:
        await user_client.connect()
        if not await user_client.is_user_authorized():
            raise RuntimeError("USER_STRING_SESSION is not authorized")
    else:
        await user_client.start(phone=config.PHONE)
    me = await user_client.get_me()
    logger.info("User client logged in as %s (%d)", me.first_name, me.id)
    # Refresh dialogs so Telethon has entity/access context for all joined
    # source channels and receives their channel updates reliably.
    dialogs = await user_client.get_dialogs()
    logger.info("Telegram dialog sync complete: %d dialogs", len(dialogs))
    await _check_mapping_access(user_client, active_mappings)
    await _remove_configured_sources(user_client)
    await _add_configured_sources(user_client)

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
    start_source_polling(user_client)

    logger.info("Both clients running. Press Ctrl+C to stop.")

    # -- Run both clients concurrently --
    await asyncio.gather(
        _run_client_forever(user_client, "User"),
        _run_client_forever(bot_client, "Bot"),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
    except Exception as exc:
        logger.exception("Fatal error: %s", exc)
        sys.exit(1)

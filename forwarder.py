"""
Core forwarding engine - runs on the USER client.

The user client sees ALL messages in every group/channel it is part of,
which is why we use it for forwarding. Telegram bots cannot see channel
posts or non-command group messages due to privacy mode.
"""

import asyncio
import logging
from datetime import datetime, timezone

from telethon import TelegramClient, events
from telethon.tl import types as tl_types
from telethon.errors import (
    FloodWaitError,
    RpcCallFailError,
    ServerError,
    TimedOutError,
)
from telethon.tl.types import (
    MessageMediaPhoto,
    MessageMediaDocument,
    MessageMediaGeo,
    MessageMediaContact,
    MessageMediaPoll,
    MessageMediaDice,
    DocumentAttributeVideo,
    DocumentAttributeAudio,
    DocumentAttributeSticker,
)

import config
import database as db

logger = logging.getLogger("forwarder")

# Telegram may temporarily reject requests during message bursts.  Retrying
# transient failures prevents a post from being lost after one failed API call.
MAX_SEND_ATTEMPTS = 5
RETRY_BASE_SECONDS = 1.0
_send_semaphore = asyncio.Semaphore(config.SEND_CONCURRENCY)

# Background forwarding tasks.  The update loop must never block on a slow
# Telegram send (flood wait / retry), otherwise every later post is delayed
# by however long the current batch takes.
_background_tasks: "set[asyncio.Task]" = set()
_claimed_messages: set[tuple[int, int]] = set()
_source_poll_task: asyncio.Task | None = None
_mapped_source_ids: set[int] = set()
_mapped_sources_loaded_at: float = 0.0
MAPPED_SOURCE_CACHE_SECONDS = 5.0


def _on_task_done(task: asyncio.Task) -> None:
    """Log failures of background forwarding tasks and release their ref."""
    _background_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        logger.exception("Background forwarding task failed", exc_info=exc)


def _spawn(coro) -> None:
    """Run a coroutine in the background without blocking the update loop."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_on_task_done)


async def shutdown_forwarding_tasks() -> None:
    """Cancel and drain in-flight forwarding tasks during application shutdown."""
    tasks = list(_background_tasks)
    if not tasks:
        return

    logger.info("Stopping %d background forwarding task(s)", len(tasks))
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    _background_tasks.clear()


def _claim_message(source_id: int, message_id: int) -> bool:
    """Claim a message once so push and polling cannot forward duplicates."""
    key = (source_id, message_id)
    if key in _claimed_messages:
        return False
    _claimed_messages.add(key)
    return True


def _is_mapped_source(event) -> bool:
    """Fast event filter: ignore updates from unrelated chats."""
    global _mapped_source_ids, _mapped_sources_loaded_at
    now = asyncio.get_running_loop().time()
    if now - _mapped_sources_loaded_at >= MAPPED_SOURCE_CACHE_SECONDS:
        _mapped_source_ids = {
            mapping["source_id"]
            for mapping in db.list_mappings()
            if mapping["active"]
        }
        _mapped_sources_loaded_at = now
    return event.chat_id in _mapped_source_ids


# ---------------------------------------------------------------------------
# Media type detection
# ---------------------------------------------------------------------------

def _get_media_type(message) -> str:
    """Return a canonical media type string for the message."""
    media = message.media
    if media is None:
        return "text"
    if isinstance(media, MessageMediaPhoto):
        return "photo"
    if isinstance(media, MessageMediaDocument):
        doc = media.document
        if doc:
            for attr in doc.attributes:
                if isinstance(attr, DocumentAttributeVideo):
                    return "video"
                if isinstance(attr, DocumentAttributeAudio):
                    return "audio"
                if isinstance(attr, DocumentAttributeSticker):
                    return "sticker"
        return "document"
    if isinstance(media, MessageMediaGeo):
        return "location"
    if isinstance(media, MessageMediaContact):
        return "contact"
    if isinstance(media, MessageMediaPoll):
        return "poll"
    if isinstance(media, MessageMediaDice):
        return "dice"
    return "unknown"


# ---------------------------------------------------------------------------
# Filter logic
# ---------------------------------------------------------------------------

def _matches_filter(message, filter_type: str, keywords: str) -> bool:
    """Check if a message passes the configured filter."""
    if filter_type and filter_type != "all":
        if _get_media_type(message) != filter_type:
            return False

    if keywords:
        text = (message.text or "").lower()
        if not text:
            return False
        keyword_list = [kw.strip().lower() for kw in keywords.split(",") if kw.strip()]
        if keyword_list and not any(kw in text for kw in keyword_list):
            return False

    return True


# ---------------------------------------------------------------------------
# Caption helpers
# ---------------------------------------------------------------------------

def _prepare_caption(message, add_caption: str, strip_caption: bool):
    """Return the modified caption (or None)."""
    if strip_caption:
        return add_caption if add_caption else None
    if add_caption:
        original = message.text or message.message or ""
        return f"{original}\n{add_caption}" if original else add_caption
    return message.text or message.message


def _is_retryable_send_error(exc: Exception) -> bool:
    """Return whether a failed Telegram send is safe to retry."""
    return isinstance(
        exc,
        (
            FloodWaitError,
            RpcCallFailError,
            ServerError,
            TimedOutError,
            ConnectionError,
            asyncio.TimeoutError,
        ),
    )


async def _send_with_retry(operation, description: str):
    """Run one Telegram send, retrying transient failures with backoff."""
    for attempt in range(1, MAX_SEND_ATTEMPTS + 1):
        try:
            async with _send_semaphore:
                return await operation()
        except FloodWaitError as exc:
            if attempt >= MAX_SEND_ATTEMPTS:
                raise
            delay = max(float(getattr(exc, "seconds", 0)), RETRY_BASE_SECONDS)
            logger.warning(
                "Telegram flood wait for %s; retry %d/%d in %.1fs",
                description, attempt, MAX_SEND_ATTEMPTS - 1, delay,
            )
        except Exception as exc:
            if not _is_retryable_send_error(exc) or attempt >= MAX_SEND_ATTEMPTS:
                raise
            delay = RETRY_BASE_SECONDS * (2 ** (attempt - 1))
            logger.warning(
                "Transient send failure for %s (%s); retry %d/%d in %.1fs",
                description, type(exc).__name__, attempt, MAX_SEND_ATTEMPTS - 1, delay,
            )
        await asyncio.sleep(delay)


# ---------------------------------------------------------------------------
# Forward one message or one grouped album to all matching destinations
# ---------------------------------------------------------------------------

async def _reply_text(client: TelegramClient, message) -> str:
    """Load text from a replied-to message, if the post quotes one."""
    reply = getattr(message, "reply_to", None)
    reply_id = getattr(reply, "reply_to_msg_id", None)
    if not reply_id:
        return ""
    try:
        replied = await client.get_messages(message.chat_id, ids=reply_id)
        return (getattr(replied, "text", None) or "").strip() if replied else ""
    except Exception:
        logger.warning("Could not load reply text for message %s", getattr(message, "id", "?"))
        return ""


async def _forward_to_destination(client: TelegramClient, messages: list, source_id: int, dest: dict) -> None:
    """Forward one batch to one destination."""
    try:
        filter_type = dest.get("filter_type", "all") or "all"
        keywords = dest.get("keywords", "") or ""
        add_caption = dest.get("add_caption", "") or ""
        strip_caption = bool(dest.get("strip_caption", 0))
        matching_messages = [message for message in messages if _matches_filter(message, filter_type, keywords)]
        if not matching_messages:
            return
        first = matching_messages[0]
        quoted_text = await _reply_text(client, first)
        original_text = first.text or first.message or ""
        if quoted_text and original_text:
            preserved_text = f"{quoted_text}\n\n{original_text}"
        else:
            preserved_text = quoted_text or original_text
        if add_caption or strip_caption:
            new_caption = _prepare_caption(first, add_caption, strip_caption)
            if quoted_text and not strip_caption:
                new_caption = f"{quoted_text}\n\n{new_caption or ''}".strip()
            if any(message.media for message in matching_messages):
                media = [message.media for message in matching_messages if message.media]
                captions = [new_caption] + [None] * (len(media) - 1)
                await _send_with_retry(lambda: client.send_file(dest["dest_id"], media, caption=captions), f"{source_id}->{dest['dest_id']}")
            else:
                await _send_with_retry(lambda: client.send_message(dest["dest_id"], new_caption or preserved_text), f"{source_id}->{dest['dest_id']}")
        else:
            # Always use Telegram native forwarding for the source header.
            await _send_with_retry(lambda: client.forward_messages(dest["dest_id"], matching_messages), f"{source_id}->{dest['dest_id']}")
            # Reply/quoted text is separate metadata; append it without
            # replacing the native Forwarded-from attribution.
            if quoted_text:
                await _send_with_retry(lambda: client.send_message(dest["dest_id"], quoted_text), f"{source_id}->{dest['dest_id']}-reply")
        logger.info("Forwarded %d message(s) | %s -> %s | source_time=%s | forwarded_at=%s", len(matching_messages), dest.get("source_name", source_id), dest.get("dest_name", dest["dest_id"]), getattr(matching_messages[0], "date", "unknown"), datetime.now(timezone.utc).isoformat())
    except Exception as exc:
        logger.error("Failed to forward to %s: %s", dest.get("dest_name", dest["dest_id"]), exc)


async def _forward_batch(client: TelegramClient, messages: list, source_id: int) -> None:
    """Forward a batch to all destinations concurrently."""
    destinations = db.get_destinations_for(source_id)
    if destinations:
        await asyncio.gather(*(
            _forward_to_destination(client, messages, source_id, dest)
            for dest in destinations
        ))


async def forward_message(client: TelegramClient, message, source_id: int) -> None:
    """Forward one non-album message."""
    await _forward_batch(client, [message], source_id)


async def forward_album(client: TelegramClient, messages: list, source_id: int) -> None:
    """Forward a Telegram media album as one grouped batch."""
    if messages:
        await _forward_batch(client, messages, source_id)


# ---------------------------------------------------------------------------
# Register the event handler on the user client
# ---------------------------------------------------------------------------

def register_forward_handler(client: TelegramClient) -> None:
    """
    Attach a NewMessage handler on the USER client.
    Skips own messages (avoid loops) and messages starting with '/'.
    """
    me_id = getattr(client, "_me_id", None)
    if config.LOG_RAW_UPDATES:
        @client.on(events.Raw)
        async def on_raw_update(update):
            """Log raw channel updates only when explicitly enabled."""
            if not isinstance(update, tl_types.UpdateNewChannelMessage):
                return
            channel_id = getattr(getattr(update, "message", None), "peer_id", None)
            channel_id = getattr(channel_id, "channel_id", None)
            source_id = -1000000000000 - channel_id if channel_id is not None else "?"
            message = getattr(update, "message", None)
            logger.info(
                "Raw channel update: type=%s source=%s message=%s",
                type(update).__name__,
                source_id,
                getattr(message, "id", "?"),
            )

    @client.on(events.NewMessage(func=_is_mapped_source))
    async def on_new_message(event: events.NewMessage.Event):
        try:
            self_posted = bool(me_id and event.sender_id == me_id)
            if self_posted and not config.FORWARD_OWN_MESSAGES:
                return

            # Grouped media is handled once by events.Album below.  Ignoring
            # its individual NewMessage events prevents an album from being
            # split into separate destination posts.
            if event.message.grouped_id:
                return

            text = event.message.text or ""
            if text.startswith("/"):
                return

            source_id = event.chat_id
            if not _claim_message(source_id, event.message.id):
                return
            logger.info(
                "Received %smessage %s from source %s | source_time=%s | received_at=%s",
                "own " if self_posted else "",
                event.message.id,
                source_id,
                getattr(event.message, "date", "unknown"),
                datetime.now(timezone.utc).isoformat(),
            )
            _spawn(forward_message(client, event.message, source_id))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Unhandled error while processing message %s from source %s",
                getattr(event.message, "id", "?"),
                getattr(event, "chat_id", "?"),
            )

    @client.on(events.Album(func=_is_mapped_source))
    async def on_album(event: events.Album.Event):
        """Forward all items in a source album in one request."""
        try:
            messages = list(event.messages)
            if not messages:
                return

            self_posted = bool(me_id and event.sender_id == me_id)
            if self_posted and not config.FORWARD_OWN_MESSAGES:
                return

            source_id = event.chat_id
            if not any(_claim_message(source_id, message.id) for message in messages):
                return

            _spawn(forward_album(client, messages, source_id))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Unhandled error while processing album from source %s",
                getattr(event, "chat_id", "?"),
            )

    logger.info("Forward handler registered on user client.")


async def _poll_mapped_sources(client: TelegramClient) -> None:
    """Poll mapped sources to cover Telegram channel push-update delays."""
    mapped_source_ids = {
        mapping["source_id"]
        for mapping in db.list_mappings()
        if mapping["active"]
    }
    # Keep all mapped channels live; polling is disabled to avoid flood waits.
    # Explicit SOURCE_POLL_SOURCE_IDS can still be used as a fallback.
    source_ids = sorted(mapped_source_ids & config.SOURCE_POLL_SOURCE_IDS)
    if not source_ids:
        logger.info("Source polling fallback disabled; relying on Telegram updates")
        return

    async def poll_source(source_id: int, initial: bool = False) -> None:
        try:
            # Keep each request small. A large sequential history scan was
            # starving live updates and caused old posts to arrive in bursts.
            messages = await client.get_messages(source_id, limit=10)
            if initial:
                for message in messages:
                    _claimed_messages.add((source_id, message.id))
                return
            for message in sorted(messages, key=lambda item: item.id):
                if getattr(message, "action", None) is not None:
                    continue
                if getattr(message, "grouped_id", None):
                    continue
                if (message.text or "").startswith("/"):
                    continue
                if not _claim_message(source_id, message.id):
                    continue
                logger.info(
                    "Polled message %s from source %s | source_time=%s",
                    message.id, source_id, getattr(message, "date", "unknown"),
                )
                _spawn(forward_message(client, message, source_id))
        except FloodWaitError as exc:
            logger.warning("Polling flood wait for %s: %.1fs", source_id, float(exc.seconds))
        except Exception:
            logger.exception("Source poll failed for %s", source_id)

    # Resolve all source heads concurrently so one slow community does not
    # block the remaining sources.
    await asyncio.gather(*(poll_source(source_id, initial=True) for source_id in source_ids))

    logger.info(
        "Source polling fallback enabled for %d source(s), interval=%.1fs",
        len(source_ids), config.SOURCE_POLL_INTERVAL_SECONDS,
    )
    while True:
        await asyncio.sleep(config.SOURCE_POLL_INTERVAL_SECONDS)
        await asyncio.gather(*(poll_source(source_id) for source_id in source_ids))


def start_source_polling(client: TelegramClient) -> None:
    """Start the mapped-source polling fallback."""
    global _source_poll_task
    if _source_poll_task is None:
        _source_poll_task = asyncio.create_task(_poll_mapped_sources(client))
        _background_tasks.add(_source_poll_task)
        _source_poll_task.add_done_callback(_on_task_done)

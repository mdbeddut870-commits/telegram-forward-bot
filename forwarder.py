"""
Core forwarding engine - runs on the USER client.

The user client sees ALL messages in every group/channel it is part of,
which is why we use it for forwarding. Telegram bots cannot see channel
posts or non-command group messages due to privacy mode.
"""

import asyncio
import hashlib
import logging
import re
from collections import OrderedDict
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
# (source_id, message_id) pairs already dispatched.  Bounded LRU so a
# long-running process cannot grow memory forever; 20k entries is plenty
# for 30 sources while live dedup (seen_posts in SQLite) covers restarts.
_CLAIMED_MAX_ENTRIES = 20000
_claimed_messages: "OrderedDict[tuple[int, int], None]" = OrderedDict()
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
        # Refresh recency so hot keys are not evicted.
        _claimed_messages.move_to_end(key)
        return False
    _claimed_messages[key] = None
    while len(_claimed_messages) > _CLAIMED_MAX_ENTRIES:
        _claimed_messages.popitem(last=False)
    return True


def _mark_claimed(source_id: int, message_id: int) -> None:
    """Pre-mark a message as seen (startup seeding); bounded like claims."""
    _claimed_messages[(source_id, message_id)] = None
    _claimed_messages.move_to_end((source_id, message_id))
    while len(_claimed_messages) > _CLAIMED_MAX_ENTRIES:
        _claimed_messages.popitem(last=False)


def _attributed_copy_text(source_name, text: str) -> str:
    """Prefix fallback copies so no post ever arrives without attribution."""
    body = (text or "").strip()
    return f"Forwarded from {source_name}\n\n{body}".strip()


def _mentions_kucoin(*texts: str) -> bool:
    """True when any original text mentions a KuCoin keyword (case-insensitive)."""
    keywords = getattr(config, "KUCOIN_KEYWORDS", ["kucoin"]) or ["kucoin"]
    for text in texts:
        lowered = (text or "").lower()
        if lowered and any(kw for kw in keywords if kw and kw in lowered):
            return True
    return False


def _with_kucoin_register_line(text: str) -> str:
    """Append the referral line once; leave non-KuCoin or already-tagged text alone."""
    body = (text or "").strip()
    register_line = getattr(config, "KUCOIN_REGISTER_LINE", "").strip()
    if not register_line or not _mentions_kucoin(body):
        return text
    if register_line in body or "CXEEW12K" in body:
        return text
    return f"{body}\n\n{register_line}".strip() if body else register_line


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
# Deduplication: skip cross-source repeats (same post in 2-3 channels)
# ---------------------------------------------------------------------------

DEDUP_MIN_TEXT_CHARS = 20  # shorter texts are too generic to dedup safely
_URL_RE = re.compile(r"https?://\S+|t\.me/\S+|@\w+", re.IGNORECASE)


def _normalize_text(text: str) -> str:
    """Lowercase, strip URLs/handles/emoji-ish noise, collapse whitespace."""
    text = (text or "").lower()
    text = _URL_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _media_fingerprint(message) -> str:
    """Best-effort stable fingerprint for media (size + largest dimension)."""
    try:
        media = getattr(message, "media", None)
        doc = getattr(media, "document", None)
        if doc is not None:
            return f"doc:{getattr(doc, 'size', '?')}"
        photo = getattr(media, "photo", None)
        if photo is not None:
            sizes = getattr(photo, "sizes", None) or []
            if sizes:
                biggest = max(sizes, key=lambda s: getattr(s, "w", 0) * getattr(s, "h", 0))
                return f"photo:{getattr(biggest, 'w', '?')}x{getattr(biggest, 'h', '?')}"
        if media is not None:
            return f"media:{type(media).__name__}"
    except Exception:
        pass
    return "nomedia"


def _content_hash(messages: list) -> "str | None":
    """Hash normalized text (+media fingerprint) of a batch. None = not dedupable."""
    parts: list[str] = []
    for message in messages:
        norm = _normalize_text(getattr(message, "text", None) or getattr(message, "message", None) or "")
        if len(norm) >= DEDUP_MIN_TEXT_CHARS:
            parts.append(f"t:{norm}")
        fingerprint = _media_fingerprint(message)
        if fingerprint != "nomedia":
            parts.append(f"m:{fingerprint}")
    if not parts:
        return None
    digest = hashlib.sha256("|".join(sorted(parts)).encode("utf-8")).hexdigest()[:32]
    return digest


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
        # Native server-side forward. Pass integer IDs + from_peer so the
        # message keeps its media/caption/album intact AND always shows the
        # native "Forwarded from" header (drop_author is never used).
        msg_ids = [message.id for message in matching_messages]
        forwarded_ok = False
        mapping_id = dest.get("mapping_id", 0)
        kucoin_mode = _mentions_kucoin(*[(m.text or "") for m in matching_messages])
        try:
            if kucoin_mode:
                # KuCoin posts skip native forward entirely: Telegram cannot
                # attach custom text to a forward, so forward+edit produced TWO
                # posts (bare forward, then link as reply when edit failed).
                # One attributed copy guarantees the link is in the FIRST post.
                copy_caption = ""
                if add_caption or strip_caption:
                    copy_caption = _prepare_caption(first, add_caption, strip_caption) or ""
                if quoted_text and not strip_caption:
                    copy_caption = f"{quoted_text}\n\n{copy_caption}".strip() if copy_caption else quoted_text
                kucoin_text = copy_caption if copy_caption else original_text
                kucoin_text = _with_kucoin_register_line(kucoin_text)
                kucoin_source = dest.get("source_name", source_id)
                kucoin_attributed = _attributed_copy_text(kucoin_source, kucoin_text)
                if matching_messages[0].media:
                    await _send_with_retry(
                        lambda: client.send_file(
                            dest["dest_id"], matching_messages[0].media,
                            caption=kucoin_attributed,
                        ),
                        f"{source_id}->{dest['dest_id']}-kucoin",
                    )
                    for message in matching_messages[1:]:
                        # Whole album belongs to a KuCoin post: force the link
                        # on every item even if only the first mentions KuCoin.
                        item_body = _with_kucoin_register_line(message.text or "")
                        if "CXEEW12K" not in item_body:
                            link_line = getattr(config, "KUCOIN_REGISTER_LINE", "").strip()
                            if link_line:
                                item_body = f"{(message.text or '').strip()}\n\n{link_line}".strip()
                        item_text = _attributed_copy_text(kucoin_source, item_body)
                        if message.media:
                            await _send_with_retry(
                                lambda m=message, t=item_text: client.send_file(
                                    dest["dest_id"], m.media, caption=t,
                                ),
                                f"{source_id}->{dest['dest_id']}-kucoin",
                            )
                        else:
                            await _send_with_retry(
                                lambda t=item_text: client.send_message(
                                    dest["dest_id"], t,
                                ),
                                f"{source_id}->{dest['dest_id']}-kucoin",
                            )
                else:
                    await _send_with_retry(
                        lambda: client.send_message(dest["dest_id"], kucoin_attributed),
                        f"{source_id}->{dest['dest_id']}-kucoin",
                    )
                logger.info("KuCoin copy sent with register link | %s -> %s | header=manual | source_time=%s | forwarded_at=%s", dest.get("source_name", source_id), dest.get("dest_name", dest["dest_id"]), getattr(first, "date", "unknown"), datetime.now(timezone.utc).isoformat())
                try:
                    db.bump_stat(mapping_id, "forwarded")
                except Exception:
                    pass
                return []
            sent = await _send_with_retry(
                lambda: client.forward_messages(dest["dest_id"], msg_ids, from_peer=source_id, drop_author=False),
                f"{source_id}->{dest['dest_id']}",
            )
            forwarded_ok = True
        except Exception as forward_exc:
            # Source blocks forwarding (protected content) or retries
            # exhausted: send a copy that still carries an explicit source
            # line, so no post ever arrives without attribution.
            logger.warning(
                "Native forward failed for %s, copying with source line: %s",
                dest.get("dest_name", dest["dest_id"]), forward_exc,
            )
            fallback_caption = ""
            if add_caption or strip_caption:
                fallback_caption = _prepare_caption(first, add_caption, strip_caption) or ""
            if quoted_text and not strip_caption:
                fallback_caption = f"{quoted_text}\n\n{fallback_caption}".strip() if fallback_caption else quoted_text
            copy_text = fallback_caption if fallback_caption else original_text
            copy_text = _with_kucoin_register_line(copy_text)
            source_name = dest.get("source_name", source_id)
            attributed = _attributed_copy_text(source_name, copy_text)
            if matching_messages[0].media:
                await _send_with_retry(
                    lambda: client.send_message(
                        dest["dest_id"], attributed, file=matching_messages[0].media,
                    ),
                    f"{source_id}->{dest['dest_id']}-copy",
                )
                for message in matching_messages[1:]:
                    if message.media:
                        await _send_with_retry(
                            lambda m=message: client.send_message(
                                dest["dest_id"],
                                _attributed_copy_text(source_name, _with_kucoin_register_line(m.text or "")),
                                file=m.media,
                            ),
                            f"{source_id}->{dest['dest_id']}-copy",
                        )
                    else:
                        await _send_with_retry(
                            lambda m=message: client.forward_messages(
                                dest["dest_id"], m.id, from_peer=source_id, drop_author=False,
                            ),
                            f"{source_id}->{dest['dest_id']}-copy",
                        )
            else:
                await _send_with_retry(
                    lambda: client.send_message(dest["dest_id"], attributed),
                    f"{source_id}->{dest['dest_id']}-copy",
                )
            logger.info("Copied %d message(s) with source line | %s -> %s | header=manual | source_time=%s | forwarded_at=%s", len(matching_messages), dest.get("source_name", source_id), dest.get("dest_name", dest["dest_id"]), getattr(matching_messages[0], "date", "unknown"), datetime.now(timezone.utc).isoformat())
            try:
                db.bump_stat(mapping_id, "forwarded")
            except Exception:
                pass
        if not forwarded_ok:
            return []
        # Caption/quote changes are applied by EDITING the just-forwarded
        # copy in place, so the "Forwarded from" header stays visible.
        # No second header-less send_message is ever emitted.
        extra_text = ""
        if add_caption or strip_caption:
            extra_text = _prepare_caption(first, add_caption, strip_caption) or ""
        if quoted_text and not strip_caption:
            extra_text = f"{quoted_text}\n\n{extra_text}".strip() if extra_text else quoted_text
        if extra_text and extra_text != original_text:
            try:
                sent_list = sent if isinstance(sent, list) else [sent]
                target = sent_list[0] if sent_list else None
                if target is not None:
                    target_id = getattr(target, "id", target)
                    try:
                        fwd_msg = await client.get_messages(dest["dest_id"], ids=target_id)
                    except Exception:
                        fwd_msg = None
                    if fwd_msg is None:
                        fwd_msg = target
                    try:
                        await _send_with_retry(
                            lambda: fwd_msg.edit(extra_text),
                            f"{source_id}->{dest['dest_id']}-edit",
                        )
                        edited_in_place = True
                    except Exception:
                        # Some forwarded copies (service/album edge cases) are
                        # not editable: resend the text so the caption still lands.
                        await _send_with_retry(
                            lambda: client.send_message(
                                dest["dest_id"], extra_text, reply_to=target_id,
                            ),
                            f"{source_id}->{dest['dest_id']}-edit-retry",
                        )
                        edited_in_place = False
                    if edited_in_place:
                        logger.info("Caption/quote edited on forwarded post | %s -> %s | header=shown", dest.get("source_name", source_id), dest.get("dest_name", dest["dest_id"]))
                    else:
                        logger.info("Caption/quote resent as reply (forward not editable) | %s -> %s | header=shown", dest.get("source_name", source_id), dest.get("dest_name", dest["dest_id"]))
            except Exception as edit_exc:
                logger.warning("Could not edit caption on forwarded post for %s (header still shown): %s", dest.get("dest_name", dest["dest_id"]), edit_exc)
        logger.info("Forwarded %d message(s) | %s -> %s | header=shown | source_time=%s | forwarded_at=%s", len(matching_messages), dest.get("source_name", source_id), dest.get("dest_name", dest["dest_id"]), getattr(matching_messages[0], "date", "unknown"), datetime.now(timezone.utc).isoformat())
        try:
            db.bump_stat(mapping_id, "forwarded")
        except Exception:
            pass
    except Exception as exc:
        try:
            db.bump_stat(dest.get("mapping_id", 0), "failed")
        except Exception:
            pass
        logger.error("Failed to forward to %s: %s", dest.get("dest_name", dest["dest_id"]), exc)


async def _forward_batch(client: TelegramClient, messages: list, source_id: int) -> None:
    """Forward a batch to all destinations concurrently."""
    destinations = db.get_destinations_for(source_id)
    if not destinations:
        return
    # Cross-source dedup: if another source already forwarded the same
    # content within 24h, skip every destination for this batch.
    dedup_on = any(bool(dest.get("dedup", 1)) for dest in destinations)
    if dedup_on:
        content_hash = _content_hash(messages)
        if content_hash and db.check_and_mark_seen(content_hash, source_id):
            for _d in destinations:
                try:
                    db.bump_stat(_d.get("mapping_id", 0), "dedup_skip")
                except Exception:
                    pass
            logger.info(
                "DEDUP skip: message %s from %s already forwarded within %dh (hash=%s)",
                [getattr(m, "id", "?") for m in messages],
                source_id, db.DEDUP_WINDOW_HOURS, content_hash,
            )
            return
    if destinations:
        await asyncio.gather(*(
            _forward_to_destination(client, messages, source_id, dest)
            for dest in destinations
        ))


async def forward_message(client: TelegramClient, message, source_id: int) -> None:
    """Forward one non-album message."""
    claimed_at = datetime.now(timezone.utc)
    await _forward_batch(client, [message], source_id)
    delay = (datetime.now(timezone.utc) - claimed_at).total_seconds()
    if delay > 10:
        logger.warning(
            "Slow forward: message %s from %s took %.1fs (queue wait + send)",
            getattr(message, "id", "?"), source_id, delay,
        )


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
    # Poll every explicitly diagnosed source. When SOURCE_POLL_ALL_MAPPED is
    # enabled, poll every active mapping so a late Telegram channel update is
    # detected within one polling interval instead of minutes later.
    if config.SOURCE_POLL_ALL_MAPPED:
        source_ids = sorted(mapped_source_ids)
    else:
        # Do not re-fetch every configured channel's history. Telegram's live
        # update stream is the fast path; the fallback is only for explicitly
        # diagnosed IDs.
        source_ids = sorted(mapped_source_ids & config.SOURCE_POLL_SOURCE_IDS)
    if not source_ids:
        logger.info("Source polling fallback disabled; relying on Telegram updates")
        return

    poll_semaphore = asyncio.Semaphore(max(1, config.SOURCE_POLL_CONCURRENCY))

    async def poll_source(source_id: int, initial: bool = False) -> None:
        try:
            # Keep each request small. A large sequential history scan was
            # starving live updates and caused old posts to arrive in bursts.
            async with poll_semaphore:
                messages = await client.get_messages(
                    source_id, limit=max(1, config.SOURCE_POLL_HISTORY_LIMIT)
                )
            if initial:
                for message in messages:
                    _mark_claimed(source_id, message.id)
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
        # Subtract the cycle duration from the sleep so the polling period
        # stays close to the configured interval.  Sleeping the full interval
        # on top of a ~13s cycle produced a ~21s detection gap.
        cycle_started = asyncio.get_running_loop().time()
        await asyncio.gather(*(poll_source(source_id) for source_id in source_ids))
        cycle_seconds = asyncio.get_running_loop().time() - cycle_started
        idle_seconds = config.SOURCE_POLL_INTERVAL_SECONDS - cycle_seconds
        if cycle_seconds > config.SOURCE_POLL_INTERVAL_SECONDS:
            logger.warning(
                "Polling cycle took %.1fs for %d source(s); longer than interval %.1fs",
                cycle_seconds, len(source_ids), config.SOURCE_POLL_INTERVAL_SECONDS,
            )
        await asyncio.sleep(max(0.5, idle_seconds))


def start_source_polling(client: TelegramClient) -> None:
    """Start the mapped-source polling fallback."""
    global _source_poll_task
    if _source_poll_task is None:
        _source_poll_task = asyncio.create_task(_poll_mapped_sources(client))
        _background_tasks.add(_source_poll_task)
        _source_poll_task.add_done_callback(_on_task_done)

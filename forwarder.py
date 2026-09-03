"""
Core forwarding engine - runs on the USER client.

The user client sees ALL messages in every group/channel it is part of,
which is why we use it for forwarding. Telegram bots cannot see channel
posts or non-command group messages due to privacy mode.
"""

import logging

from telethon import TelegramClient, events
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

import database as db

logger = logging.getLogger("forwarder")


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


# ---------------------------------------------------------------------------
# Forward one message or one grouped album to all matching destinations
# ---------------------------------------------------------------------------

async def _forward_batch(client: TelegramClient, messages: list, source_id: int) -> None:
    """Forward messages as one Telegram request, preserving media albums."""
    destinations = db.get_destinations_for(source_id)
    if not destinations:
        return

    for dest in destinations:
        try:
            filter_type = dest.get("filter_type", "all") or "all"
            keywords = dest.get("keywords", "") or ""
            add_caption = dest.get("add_caption", "") or ""
            strip_caption = bool(dest.get("strip_caption", 0))

            matching_messages = [
                message for message in messages
                if _matches_filter(message, filter_type, keywords)
            ]
            if not matching_messages:
                continue

            if add_caption or strip_caption:
                # Telegram albums have one caption, normally on the first
                # item.  Keep that caption on the first forwarded media and
                # leave the remaining album items captionless.
                first = matching_messages[0]
                new_caption = _prepare_caption(first, add_caption, strip_caption)
                if any(message.media for message in matching_messages):
                    media = [message.media for message in matching_messages if message.media]
                    captions = [new_caption] + [None] * (len(media) - 1)
                    await client.send_file(
                        dest["dest_id"], media, caption=captions,
                    )
                else:
                    await client.send_message(
                        dest["dest_id"], new_caption or first.text,
                    )
            else:
                # Passing the complete list in one API request is what keeps
                # grouped photos/videos grouped at the destination.
                await client.forward_messages(dest["dest_id"], matching_messages)

            logger.info(
                "Forwarded %d message(s) | %s -> %s",
                len(matching_messages),
                dest.get("source_name", source_id),
                dest.get("dest_name", dest["dest_id"]),
            )
        except Exception as exc:
            logger.error(
                "Failed to forward to %s: %s",
                dest.get("dest_name", dest["dest_id"]),
                exc,
            )


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

    @client.on(events.NewMessage)
    async def on_new_message(event: events.NewMessage.Event):
        if me_id and event.sender_id == me_id:
            return

        # Grouped media is handled once by events.Album below.  Ignoring its
        # individual NewMessage events prevents an album from being split into
        # separate destination posts.
        if event.message.grouped_id:
            return

        text = event.message.text or ""
        if text.startswith("/"):
            return

        source_id = event.chat_id
        destinations = db.get_destinations_for(source_id)
        if not destinations:
            return

        await forward_message(client, event.message, source_id)

    @client.on(events.Album)
    async def on_album(event: events.Album.Event):
        """Forward all items in a source album in one request."""
        if me_id and event.sender_id == me_id:
            return

        messages = list(event.messages)
        if not messages:
            return

        source_id = event.chat_id
        if not db.get_destinations_for(source_id):
            return

        await forward_album(client, messages, source_id)

    logger.info("Forward handler registered on user client.")

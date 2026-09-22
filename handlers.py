"""
Bot command handlers - runs on the BOT client.

The bot client handles admin commands (/add, /remove, /list, etc.)
while the user client handles actual message forwarding.
"""

import logging

from telethon import TelegramClient, events

import config
import database as db

logger = logging.getLogger("handlers")


def _is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS


async def _resolve_name(client: TelegramClient, entity_id: int) -> str:
    """Try to get a human-readable name for a chat/channel."""
    try:
        entity = await client.get_entity(entity_id)
        if hasattr(entity, "title"):
            return entity.title
        if hasattr(entity, "first_name"):
            name = entity.first_name or ""
            if entity.last_name:
                name += f" {entity.last_name}"
            return name.strip()
    except Exception:
        pass
    return str(entity_id)


# ---------------------------------------------------------------------------
# Register all handlers on the BOT client
# ---------------------------------------------------------------------------

def register_bot_handlers(bot_client: TelegramClient) -> None:
    """Attach all command handlers to the bot client."""

    # -- /start ---------------------------------------------------------
    @bot_client.on(events.NewMessage(pattern=r"^/start$"))
    async def cmd_start(event: events.NewMessage.Event):
        if not _is_admin(event.sender_id):
            await event.reply("You are not authorized to use this bot.")
            return

        await event.reply(
            "**Telegram Auto-Forward Bot**\n\n"
            "I forward messages from source channels/groups to unlimited "
            "destinations.\n\n"
            "**How it works:**\n"
            "1. Add a mapping: `/add <source_id> <dest_id>`\n"
            "2. Every new message is auto-forwarded!\n\n"
            "**Commands:**\n"
            "/add - Add source to destination mapping\n"
            "/remove - Remove a mapping\n"
            "/toggle - Pause / resume a mapping\n"
            "/list - List all mappings\n"
            "/filter - Set message type/keyword filter\n"
            "/caption - Set custom caption\n"
            "/dedup - Toggle duplicate skipping per mapping\n"
            "/header - Show/hide the 'Forwarded from' header per mapping\n"
            "/stats - Show statistics\n"
            "/help - Show this message\n"
        )

    # -- /help ----------------------------------------------------------
    @bot_client.on(events.NewMessage(pattern=r"^/help$"))
    async def cmd_help(event: events.NewMessage.Event):
        if not _is_admin(event.sender_id):
            return
        await cmd_start(event)

    # -- /add <source_id> <dest_id> -------------------------------------
    @bot_client.on(events.NewMessage(pattern=r"^/add\s+(.+)$"))
    async def cmd_add(event: events.NewMessage.Event):
        if not _is_admin(event.sender_id):
            return

        parts = event.pattern_match.group(1).strip().split()
        if len(parts) < 2:
            await event.reply(
                "**Usage:** `/add <source_id> <dest_id>`\n\n"
                "Find IDs by forwarding a message to @userinfobot or "
                "@getidsbot.\n\n"
                "**Example:** `/add -1001234567890 -1009876543210`"
            )
            return

        try:
            source_id = int(parts[0])
            dest_id = int(parts[1])
        except ValueError:
            await event.reply("Both IDs must be integers.")
            return

        source_name = await _resolve_name(bot_client, source_id)
        dest_name = await _resolve_name(bot_client, dest_id)

        row_id = db.add_mapping(source_id, dest_id, source_name, dest_name)
        if row_id:
            await event.reply(
                f"**Mapping #{row_id} created!**\n\n"
                f"Source: {source_name} (`{source_id}`)\n"
                f"Dest: {dest_name} (`{dest_id}`)\n\n"
                f"Messages from source will now be auto-forwarded."
            )
        else:
            await event.reply("This mapping already exists.")

    # -- /remove <mapping_id> -------------------------------------------
    @bot_client.on(events.NewMessage(pattern=r"^/remove\s+(\d+)$"))
    async def cmd_remove(event: events.NewMessage.Event):
        if not _is_admin(event.sender_id):
            return

        mapping_id = int(event.pattern_match.group(1))
        if db.remove_mapping(mapping_id):
            await event.reply(f"Mapping **#{mapping_id}** removed.")
        else:
            await event.reply(f"Mapping #{mapping_id} not found.")

    # -- /toggle <mapping_id> -------------------------------------------
    @bot_client.on(events.NewMessage(pattern=r"^/toggle\s+(\d+)$"))
    async def cmd_toggle(event: events.NewMessage.Event):
        if not _is_admin(event.sender_id):
            return

        mapping_id = int(event.pattern_match.group(1))
        new_state = db.toggle_mapping(mapping_id)
        if new_state is None:
            await event.reply(f"Mapping #{mapping_id} not found.")
        else:
            status = "**Active**" if new_state else "**Paused**"
            await event.reply(f"Mapping **#{mapping_id}** is now {status}.")

    # -- /list ----------------------------------------------------------
    @bot_client.on(events.NewMessage(pattern=r"^/list$"))
    async def cmd_list(event: events.NewMessage.Event):
        if not _is_admin(event.sender_id):
            return

        mappings = db.list_mappings()
        if not mappings:
            await event.reply("No mappings configured. Use /add to create one.")
            return

        lines = ["**All Mappings:**\n"]
        for m in mappings:
            status = "ON" if m["active"] else "OFF"
            header_tag = " [no-header]" if m.get("hide_header") else ""
            lines.append(
                f"[{status}]{header_tag} **#{m['id']}**\n"
                f"  Source: {m['source_name']} (`{m['source_id']}`)\n"
                f"  Dest: {m['dest_name']} (`{m['dest_id']}`)\n"
            )
        lines.append("Use /toggle <id> to pause/resume, /remove <id> to delete.")
        await event.reply("\n".join(lines))

    # -- /filter <mapping_id> <type> [keywords] -------------------------
    @bot_client.on(events.NewMessage(pattern=r"^/filter\s+(\d+)\s+(\w+)(.*)$"))
    async def cmd_filter(event: events.NewMessage.Event):
        if not _is_admin(event.sender_id):
            return

        mapping_id = int(event.pattern_match.group(1))
        filter_type = event.pattern_match.group(2).strip().lower()
        keywords = event.pattern_match.group(3).strip()

        valid_types = {
            "all", "text", "photo", "video", "document", "audio",
            "sticker", "location", "contact", "poll",
        }
        if filter_type not in valid_types:
            await event.reply(
                f"Invalid filter type `{filter_type}`.\n"
                f"Valid: {', '.join(sorted(valid_types))}"
            )
            return

        mapping = db.get_mapping(mapping_id)
        if not mapping:
            await event.reply(f"Mapping #{mapping_id} not found.")
            return

        kwargs: dict = {"filter_type": filter_type}
        if keywords:
            kwargs["keywords"] = keywords

        db.update_filter(mapping_id, **kwargs)

        kw_text = f"\n  Keywords: `{keywords}`" if keywords else ""
        await event.reply(
            f"**Filter updated for #{mapping_id}**\n"
            f"  Type: `{filter_type}`{kw_text}"
        )

    # -- /caption <mapping_id> [strip] <text> ---------------------------
    @bot_client.on(events.NewMessage(pattern=r"^/caption\s+(\d+)\s+(.+)$"))
    async def cmd_caption(event: events.NewMessage.Event):
        if not _is_admin(event.sender_id):
            return

        mapping_id = int(event.pattern_match.group(1))
        rest = event.pattern_match.group(2).strip()

        strip = False
        if rest.lower().startswith("strip"):
            strip = True
            rest = rest[5:].strip()

        mapping = db.get_mapping(mapping_id)
        if not mapping:
            await event.reply(f"Mapping #{mapping_id} not found.")
            return

        kwargs: dict = {"strip_caption": int(strip)}
        if rest:
            kwargs["add_caption"] = rest

        db.update_filter(mapping_id, **kwargs)

        mode = "replaced" if strip else "appended"
        await event.reply(
            f"**Caption {mode} for #{mapping_id}**\n"
            f"  Caption: `{rest or '(removed)'}`"
        )

    # -- /dedup <mapping_id> <on|off> ------------------------------------
    @bot_client.on(events.NewMessage(pattern=r"^/dedup\s+(\d+)\s+(on|off)$"))
    async def cmd_dedup(event: events.NewMessage.Event):
        if not _is_admin(event.sender_id):
            return

        mapping_id = int(event.pattern_match.group(1))
        enable = event.pattern_match.group(2).lower() == "on"

        mapping = db.get_mapping(mapping_id)
        if not mapping:
            await event.reply(f"Mapping #{mapping_id} not found.")
            return

        db.update_filter(mapping_id, dedup=int(enable))
        state = "**ON** (repeats skipped)" if enable else "**OFF** (all posts forwarded)"
        await event.reply(f"Deduplication for mapping **#{mapping_id}** is now {state}.")

    # -- /header <mapping_id> <hide|show> ---------------------------------
    @bot_client.on(events.NewMessage(pattern=r"^/header\s+(\d+)\s+(hide|show)$"))
    async def cmd_header(event: events.NewMessage.Event):
        if not _is_admin(event.sender_id):
            return

        mapping_id = int(event.pattern_match.group(1))
        hide = event.pattern_match.group(2).lower() == "hide"

        mapping = db.get_mapping(mapping_id)
        if not mapping:
            await event.reply(f"Mapping #{mapping_id} not found.")
            return

        db.update_filter(mapping_id, hide_header=int(hide))
        state = "**HIDDEN** (no 'Forwarded from' line)" if hide else "**SHOWN** (normal forward header)"
        await event.reply(f"Forward header for mapping **#{mapping_id}** is now {state}.")

    # -- /stats ---------------------------------------------------------
    @bot_client.on(events.NewMessage(pattern=r"^/stats$"))
    async def cmd_stats(event: events.NewMessage.Event):
        if not _is_admin(event.sender_id):
            return

        mappings = db.list_mappings()
        active = sum(1 for m in mappings if m["active"])
        paused = len(mappings) - active

        await event.reply(
            "**Bot Statistics**\n\n"
            f"  Total mappings: **{len(mappings)}**\n"
            f"  Active: **{active}**\n"
            f"  Paused: **{paused}**\n"
        )

    logger.info("Bot command handlers registered.")

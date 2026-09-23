"""
SQLite database layer - persists channel mappings, filters, and bot state.
"""

import os
import sqlite3
from typing import Optional

from config import DB_PATH


def _ensure_dir() -> None:
    parent = os.path.dirname(DB_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)


def get_conn() -> sqlite3.Connection:
    _ensure_dir()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# -- Schema --

def init_db() -> None:
    """Create tables if they don't exist."""
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS mappings (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id   INTEGER NOT NULL,
                dest_id     INTEGER NOT NULL,
                source_name TEXT    NOT NULL DEFAULT '',
                dest_name   TEXT    NOT NULL DEFAULT '',
                active      INTEGER NOT NULL DEFAULT 1,
                created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
                UNIQUE(source_id, dest_id)
            );

            CREATE TABLE IF NOT EXISTS filters (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                mapping_id    INTEGER NOT NULL REFERENCES mappings(id) ON DELETE CASCADE,
                filter_type   TEXT    NOT NULL DEFAULT 'all',
                keywords      TEXT    NOT NULL DEFAULT '',
                add_caption   TEXT    NOT NULL DEFAULT '',
                strip_caption INTEGER NOT NULL DEFAULT 0,
                hide_header   INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS bot_state (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS seen_posts (
                content_hash TEXT PRIMARY KEY,
                source_id    INTEGER NOT NULL DEFAULT 0,
                first_seen   TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS fwd_stats (
                day         TEXT NOT NULL,
                mapping_id  INTEGER NOT NULL DEFAULT 0,
                forwarded   INTEGER NOT NULL DEFAULT 0,
                failed      INTEGER NOT NULL DEFAULT 0,
                dedup_skip  INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (day, mapping_id)
            );
        """)
    # Migrations run AFTER the schema block, OUTSIDE the with: each
    # migration opens its own connection (no nested get_conn()).
    _migrate_dedup_column()
    _migrate_hide_header_column()


# -- Mapping CRUD --

def add_mapping(
    source_id: int,
    dest_id: int,
    source_name: str = "",
    dest_name: str = "",
) -> int:
    """Insert a new source->dest mapping. Returns the mapping id (0 if duplicate)."""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO mappings "
            "(source_id, dest_id, source_name, dest_name) "
            "VALUES (?, ?, ?, ?)",
            (source_id, dest_id, source_name, dest_name),
        )
        if cur.lastrowid:
            conn.execute(
                "INSERT INTO filters (mapping_id) VALUES (?)",
                (cur.lastrowid,),
            )
        return cur.lastrowid


def remove_mapping(mapping_id: int) -> bool:
    """Delete a mapping by id. Returns True if deleted."""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM mappings WHERE id = ?", (mapping_id,))
        return cur.rowcount > 0


def toggle_mapping(mapping_id: int) -> Optional[bool]:
    """Toggle active flag. Returns new state or None if not found."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT active FROM mappings WHERE id = ?", (mapping_id,)
        ).fetchone()
        if not row:
            return None
        new_state = 0 if row["active"] else 1
        conn.execute(
            "UPDATE mappings SET active = ? WHERE id = ?",
            (new_state, mapping_id),
        )
        return bool(new_state)


def list_mappings() -> list[dict]:
    """Return all mappings ordered by id, with the header-hide flag."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT m.*, f.hide_header "
            "FROM mappings m LEFT JOIN filters f ON f.mapping_id = m.id "
            "ORDER BY m.id"
        ).fetchall()
        return [dict(r) for r in rows]


def get_mapping(mapping_id: int) -> Optional[dict]:
    """Return a single mapping with its filter, or None."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT m.*, f.filter_type, f.keywords, f.add_caption, f.strip_caption, f.hide_header "
            "FROM mappings m "
            "LEFT JOIN filters f ON f.mapping_id = m.id "
            "WHERE m.id = ?",
            (mapping_id,),
        ).fetchone()
        return dict(row) if row else None


def get_destinations_for(source_id: int) -> list[dict]:
    """Return all active destinations for a given source chat."""
    # Migrations first: the live DB may predate the dedup/hide_header columns.
    _migrate_dedup_column()
    _migrate_hide_header_column()
    with get_conn() as conn:
        # Single query returns mapping + filter toggles (dedup, hide_header).
        # No nested get_conn(): everything reuses the outer connection.
        rows = conn.execute(
            "SELECT m.*, f.filter_type, f.keywords, f.add_caption, f.strip_caption, "
            "COALESCE(f.hide_header, 0) AS hide_header, "
            "COALESCE(f.dedup, 1) AS dedup "
            "FROM mappings m "
            "LEFT JOIN filters f ON f.mapping_id = m.id "
            "WHERE m.source_id = ? AND m.active = 1",
            (source_id,),
        ).fetchall()
        return [dict(r) for r in rows]


# -- Filter CRUD --

def update_filter(mapping_id: int, **kwargs) -> bool:
    """
    Update filter for a mapping.
    Accepted kwargs: filter_type, keywords, add_caption, strip_caption, dedup, hide_header
    """
    allowed = {"filter_type", "keywords", "add_caption", "strip_caption", "dedup", "hide_header"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return False
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [mapping_id]
    with get_conn() as conn:
        cur = conn.execute(
            f"UPDATE filters SET {set_clause} WHERE mapping_id = ?",
            values,
        )
        return cur.rowcount > 0


def remove_mappings_for_source(source_id: int) -> int:
    """Delete all mappings for one source chat."""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM mappings WHERE source_id = ?", (source_id,))
        return cur.rowcount


def normalize_all_destination_ids() -> int:
    """Convert legacy positive -100... destination IDs in-place."""
    changed = 0
    with get_conn() as conn:
        rows = conn.execute("SELECT id, dest_id FROM mappings").fetchall()
        for row in rows:
            dest_id = int(row["dest_id"])
            if dest_id >= 10**12:
                canonical = -dest_id
                duplicate = conn.execute(
                    "SELECT id FROM mappings WHERE source_id = (SELECT source_id FROM mappings WHERE id = ?) AND dest_id = ? AND id != ?",
                    (row["id"], canonical, row["id"]),
                ).fetchone()
                if duplicate:
                    conn.execute("DELETE FROM mappings WHERE id = ?", (row["id"],))
                else:
                    conn.execute("UPDATE mappings SET dest_id = ? WHERE id = ?", (canonical, row["id"]))
                changed += 1
    return changed


def remove_mappings_for_sources(source_ids: set[int]) -> int:
    """Delete all mappings for the given source chats."""
    if not source_ids:
        return 0
    with get_conn() as conn:
        placeholders = ",".join("?" for _ in source_ids)
        cur = conn.execute(f"DELETE FROM mappings WHERE source_id IN ({placeholders})", tuple(source_ids))
        return cur.rowcount


# -- Deduplication: cross-source repeat detection ----------------------

DEDUP_WINDOW_HOURS = 24


def _migrate_dedup_column() -> None:
    """Add filters.dedup to databases created before dedup existed."""
    try:
        with get_conn() as conn:
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(filters)")}
            if "dedup" not in cols:
                conn.execute("ALTER TABLE filters ADD COLUMN dedup INTEGER NOT NULL DEFAULT 1")
    except Exception:
        pass  # best-effort migration; forward path still works without it


def _migrate_hide_header_column() -> None:
    """Add filters.hide_header to databases created before it existed."""
    try:
        with get_conn() as conn:
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(filters)")}
            if "hide_header" not in cols:
                conn.execute("ALTER TABLE filters ADD COLUMN hide_header INTEGER NOT NULL DEFAULT 0")
    except Exception:
        pass  # best-effort migration; forward path still works without it


def _get_filter_hide_header(conn, mapping_id: int) -> bool:
    """Per-mapping hide_header toggle; defaults to False (header shown) when missing."""
    try:
        row = conn.execute(
            "SELECT hide_header FROM filters WHERE mapping_id = ?", (mapping_id,)
        ).fetchone()
        return bool(row["hide_header"]) if row is not None else False
    except Exception:
        return False


def _get_filter_dedup(conn, mapping_id: int) -> bool:
    """Per-mapping dedup toggle; defaults to ON when column/row is missing."""
    try:
        row = conn.execute(
            "SELECT dedup FROM filters WHERE mapping_id = ?", (mapping_id,)
        ).fetchone()
        return row is None or bool(row["dedup"])
    except Exception:
        return True


def prune_seen_posts(max_age_hours: int = DEDUP_WINDOW_HOURS) -> int:
    """Delete hashes older than the window. Returns rows removed."""
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM seen_posts WHERE first_seen < datetime('now', ?)",
            (f"-{max_age_hours} hours",),
        )
        return cur.rowcount


def check_and_mark_seen(content_hash: str, source_id: int) -> bool:
    """Return True if hash was already seen (duplicate), else record and return False."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM seen_posts WHERE content_hash = ? AND "
            "first_seen >= datetime('now', ?)",
            (content_hash, f"-{DEDUP_WINDOW_HOURS} hours"),
        ).fetchone()
        if row:
            return True
        # Row may exist outside the 24h window: INSERT OR IGNORE keeps the
        # ORIGINAL first_seen forever so the window never slides forward.
        conn.execute(
            "INSERT OR IGNORE INTO seen_posts (content_hash, source_id) VALUES (?, ?)",
            (content_hash, source_id),
        )
        return False


# -- Forwarding stats (per-day, per-mapping counters) ------------------

def _today() -> str:
    from datetime import date
    return date.today().isoformat()


def bump_stat(mapping_id: int, field: str, day: str = "") -> None:
    """Increment a fwd_stats counter. Field: forwarded|failed|dedup_skip."""
    if field not in ("forwarded", "failed", "dedup_skip"):
        raise ValueError(f"unknown stat field: {field}")
    day = day or _today()
    with get_conn() as conn:
        conn.execute(
            f"INSERT INTO fwd_stats (day, mapping_id, {field}) VALUES (?, ?, 1) "
            f"ON CONFLICT(day, mapping_id) DO UPDATE SET {field} = {field} + 1",
            (day, mapping_id),
        )


def get_stats(days: int = 7) -> dict:
    """Aggregate fwd_stats for the last N days: totals + per-day + top mappings."""
    with get_conn() as conn:
        totals = conn.execute(
            "SELECT COALESCE(SUM(forwarded),0) f, COALESCE(SUM(failed),0) fa, "
            "COALESCE(SUM(dedup_skip),0) d FROM fwd_stats "
            "WHERE day >= date('now', ?)",
            (f"-{max(1, days)} days",),
        ).fetchone()
        per_day = conn.execute(
            "SELECT day, SUM(forwarded) f, SUM(failed) fa, SUM(dedup_skip) d "
            "FROM fwd_stats WHERE day >= date('now', ?) GROUP BY day ORDER BY day",
            (f"-{max(1, days)} days",),
        ).fetchall()
        top = conn.execute(
            "SELECT mapping_id, SUM(forwarded) f, SUM(failed) fa FROM fwd_stats "
            "WHERE day >= date('now', ?) GROUP BY mapping_id ORDER BY f DESC LIMIT 5",
            (f"-{max(1, days)} days",),
        ).fetchall()
    return {
        "forwarded": totals["f"],
        "failed": totals["fa"],
        "dedup_skip": totals["d"],
        "per_day": [dict(r) for r in per_day],
        "top": [dict(r) for r in top],
    }


# -- Web dashboard: recent dedup entries -------------------------------

def recent_seen(limit: int = 30) -> list[dict]:
    """Newest-first sample of the dedup table for the dashboard."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT content_hash, source_id, first_seen FROM seen_posts "
            "ORDER BY first_seen DESC LIMIT ?",
            (max(1, min(200, int(limit))),),
        ).fetchall()
        return [dict(r) for r in rows]


# -- Bot state (key-value store) --

def get_state(key: str, default: str = "") -> str:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM bot_state WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default


def set_state(key: str, value: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO bot_state (key, value) VALUES (?, ?)",
            (key, value),
        )

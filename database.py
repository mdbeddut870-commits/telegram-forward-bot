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
                strip_caption INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS bot_state (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)


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
    """Return all mappings ordered by id."""
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM mappings ORDER BY id").fetchall()
        return [dict(r) for r in rows]


def get_mapping(mapping_id: int) -> Optional[dict]:
    """Return a single mapping with its filter, or None."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT m.*, f.filter_type, f.keywords, f.add_caption, f.strip_caption "
            "FROM mappings m "
            "LEFT JOIN filters f ON f.mapping_id = m.id "
            "WHERE m.id = ?",
            (mapping_id,),
        ).fetchone()
        return dict(row) if row else None


def get_destinations_for(source_id: int) -> list[dict]:
    """Return all active destinations for a given source chat."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT m.*, f.filter_type, f.keywords, f.add_caption, f.strip_caption "
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
    Accepted kwargs: filter_type, keywords, add_caption, strip_caption
    """
    allowed = {"filter_type", "keywords", "add_caption", "strip_caption"}
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


def normalize_destination_ids(dest_id: int) -> int:
    """Normalize legacy positive Telegram supergroup IDs and remove duplicates."""
    canonical = -abs(dest_id) if abs(dest_id) >= 10**12 else dest_id
    with get_conn() as conn:
        if canonical != dest_id:
            duplicate = conn.execute(
                "SELECT id FROM mappings WHERE source_id = ? AND dest_id = ?",
                (None, canonical),
            ).fetchone()
            conn.execute("UPDATE mappings SET dest_id = ? WHERE dest_id = ?", (canonical, dest_id))
        return canonical


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

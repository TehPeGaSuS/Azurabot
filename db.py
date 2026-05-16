"""
db.py — SQLite schema and async helpers.
Only runtime state lives here: channels, per-channel settings, announce log.
Network definitions always come from config.toml.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncIterator

import aiosqlite

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    network_name     TEXT    NOT NULL,
    channel          TEXT    NOT NULL,
    announce_mode    TEXT    NOT NULL DEFAULT 'live',
    show_listeners   INTEGER NOT NULL DEFAULT 1,
    show_dj          INTEGER NOT NULL DEFAULT 1,
    enabled          INTEGER NOT NULL DEFAULT 1,
    last_announced_at TEXT,
    added_at         TEXT    NOT NULL,
    UNIQUE(network_name, channel)
);

CREATE TABLE IF NOT EXISTS announce_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    network_name TEXT NOT NULL,
    channel      TEXT NOT NULL,
    song_id      TEXT NOT NULL,
    announced_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_announce_log_lookup
    ON announce_log(network_name, channel, song_id, announced_at);
"""


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._db.commit()
        log.info("Database connected: %s", self.path)

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._lock:
            try:
                yield self._db
                await self._db.commit()
            except Exception:
                await self._db.rollback()
                raise

    # ------------------------------------------------------------------ #
    # Channels
    # ------------------------------------------------------------------ #

    async def add_channel(
        self,
        network_name: str,
        channel: str,
        announce_mode: str = "live",
    ) -> int | None:
        """Insert a channel. Returns new row id, or None if already exists."""
        now = _now()
        try:
            async with self.transaction() as db:
                cur = await db.execute(
                    """
                    INSERT INTO channels
                        (network_name, channel, announce_mode, added_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (network_name, channel, announce_mode, now),
                )
                return cur.lastrowid
        except aiosqlite.IntegrityError:
            return None

    async def remove_channel(self, network_name: str, channel: str) -> bool:
        async with self.transaction() as db:
            cur = await db.execute(
                "DELETE FROM channels WHERE network_name=? AND channel=?",
                (network_name, channel),
            )
            return cur.rowcount > 0

    async def get_channel(
        self, network_name: str, channel: str
    ) -> aiosqlite.Row | None:
        async with self._lock:
            cur = await self._db.execute(
                "SELECT * FROM channels WHERE network_name=? AND channel=?",
                (network_name, channel),
            )
            return await cur.fetchone()

    async def get_all_channels(self) -> list[aiosqlite.Row]:
        async with self._lock:
            cur = await self._db.execute(
                "SELECT * FROM channels ORDER BY network_name, channel"
            )
            return await cur.fetchall()

    async def get_network_channels(self, network_name: str) -> list[aiosqlite.Row]:
        async with self._lock:
            cur = await self._db.execute(
                "SELECT * FROM channels WHERE network_name=? ORDER BY channel",
                (network_name,),
            )
            return await cur.fetchall()

    async def get_enabled_channels(self) -> list[aiosqlite.Row]:
        async with self._lock:
            cur = await self._db.execute(
                "SELECT * FROM channels WHERE enabled=1"
            )
            return await cur.fetchall()

    async def set_channel_field(
        self, network_name: str, channel: str, field: str, value
    ) -> bool:
        allowed = {
            "announce_mode", "show_listeners", "show_dj",
            "enabled", "last_announced_at",
        }
        if field not in allowed:
            raise ValueError(f"Field '{field}' is not settable")
        async with self.transaction() as db:
            cur = await db.execute(
                f"UPDATE channels SET {field}=? WHERE network_name=? AND channel=?",
                (value, network_name, channel),
            )
            return cur.rowcount > 0

    # ------------------------------------------------------------------ #
    # Announce log
    # ------------------------------------------------------------------ #

    async def was_recently_announced(
        self,
        network_name: str,
        channel: str,
        song_id: str,
        window_sec: int,
    ) -> bool:
        async with self._lock:
            cur = await self._db.execute(
                """
                SELECT 1 FROM announce_log
                WHERE network_name=? AND channel=? AND song_id=?
                  AND announced_at >= datetime('now', ?)
                LIMIT 1
                """,
                (network_name, channel, song_id, f"-{window_sec} seconds"),
            )
            return await cur.fetchone() is not None

    async def log_announce(
        self, network_name: str, channel: str, song_id: str
    ) -> None:
        now = _now()
        async with self.transaction() as db:
            await db.execute(
                """
                INSERT INTO announce_log (network_name, channel, song_id, announced_at)
                VALUES (?, ?, ?, ?)
                """,
                (network_name, channel, song_id, now),
            )

    async def prune_announce_log(self, older_than_sec: int) -> int:
        async with self.transaction() as db:
            cur = await db.execute(
                "DELETE FROM announce_log WHERE announced_at < datetime('now', ?)",
                (f"-{older_than_sec} seconds",),
            )
            return cur.rowcount


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

"""
dispatcher.py — Consumes SongEvents from the queue and fans out
announcements to all enabled channels, respecting each channel's
announce_mode and dedup rules.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone

from announcer import format_message
from config import AnnounceConfig
from db import Database
from webhook import SongEvent

log = logging.getLogger(__name__)

_INTERVAL_RE = re.compile(r"^(\d+)(m|h)$")


def _parse_mode(mode: str) -> tuple[str, int]:
    """
    Returns ('live', 0) or ('interval', seconds).
    'hourly' is an alias for '1h'.
    """
    if mode == "live":
        return ("live", 0)
    if mode == "hourly":
        return ("interval", 3600)
    m = _INTERVAL_RE.match(mode)
    if m:
        value, unit = int(m.group(1)), m.group(2)
        seconds = value * 60 if unit == "m" else value * 3600
        return ("interval", seconds)
    log.warning("Unknown announce_mode '%s', defaulting to live", mode)
    return ("live", 0)


class Dispatcher:
    def __init__(
        self,
        queue: asyncio.Queue,
        db: Database,
        announce_cfg: AnnounceConfig,
        irc_manager,  # irc.manager.IRCManager — avoid circular import
    ) -> None:
        self.queue = queue
        self.db = db
        self.cfg = announce_cfg
        self.irc = irc_manager
        self._task: asyncio.Task | None = None
        self._prune_task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="dispatcher")
        self._prune_task = asyncio.create_task(self._prune_loop(), name="dispatcher-prune")
        log.info("Dispatcher started")

    async def stop(self) -> None:
        for t in (self._task, self._prune_task):
            if t:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass

    async def _run(self) -> None:
        while True:
            event: SongEvent = await self.queue.get()
            log.debug("Dispatcher received: %s - %s", event.artist, event.title)
            channels = await self.db.get_enabled_channels()
            for row in channels:
                asyncio.create_task(
                    self._process_channel(row, event),
                    name=f"announce-{row['network_name']}-{row['channel']}",
                )

    async def _process_channel(self, row, event: SongEvent) -> None:
        network_name: str = row["network_name"]
        channel: str = row["channel"]
        mode_str: str = row["announce_mode"] or "live"
        kind, interval_sec = _parse_mode(mode_str)

        if kind == "interval":
            last_raw = row["last_announced_at"]
            if last_raw:
                try:
                    last_dt = datetime.fromisoformat(last_raw)
                    elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
                    if elapsed < interval_sec:
                        log.debug(
                            "Skipping %s/%s — interval not elapsed (%.0fs remaining)",
                            network_name, channel, interval_sec - elapsed,
                        )
                        return
                except ValueError:
                    pass  # Malformed timestamp — proceed

        # Dedup check
        already = await self.db.was_recently_announced(
            network_name, channel, event.song_id, self.cfg.dedup_window_sec
        )
        if already:
            log.debug("Skipping %s/%s — duplicate song_id %s", network_name, channel, event.song_id)
            return

        # Send
        message = format_message(event, self.cfg)
        sent = await self.irc.send(network_name, channel, message)
        if not sent:
            log.warning("Could not send to %s/%s — network not connected", network_name, channel)
            return

        now_iso = datetime.now(timezone.utc).isoformat()
        await self.db.log_announce(network_name, channel, event.song_id)
        await self.db.set_channel_field(network_name, channel, "last_announced_at", now_iso)
        log.info("Announced to %s/%s: %s - %s", network_name, channel, event.artist, event.title)

    async def _prune_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.prune_interval_sec)
            count = await self.db.prune_announce_log(self.cfg.prune_older_than_sec)
            if count:
                log.debug("Pruned %d old announce log entries", count)

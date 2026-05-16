"""
commands/channel_commands.py — Public channel commands (!np, !next).

Anyone in a registered channel can trigger these. Rate limited per channel
with a configurable cooldown (shared across all commands in that channel).
Hits are silently ignored.
"""

from __future__ import annotations

import logging
import time

from announcer import format_message
from config import CommandsConfig, AnnounceConfig
from webhook import SongEvent

log = logging.getLogger(__name__)


class ChannelCommandHandler:
    def __init__(
        self,
        commands_cfg: CommandsConfig,
        announce_cfg: AnnounceConfig,
        irc_manager,       # irc.manager.IRCManager
        last_event: list,  # [SongEvent | None]
    ) -> None:
        self.cmd_cfg = commands_cfg
        self.ann_cfg = announce_cfg
        self.irc = irc_manager
        self.last_event = last_event

        # (network_name, channel) -> last trigger timestamp
        self._cooldowns: dict[tuple[str, str], float] = {}

    async def handle(self, network_name: str, channel: str, mask: str, message: str) -> None:
        """Called for every PRIVMSG in a channel. Checks for trigger prefix."""
        message = message.strip()
        trigger = self.cmd_cfg.trigger

        if not message.startswith(trigger):
            return

        command = message[len(trigger):].split()[0].lower()
        if command not in ("np", "nowplaying", "next"):
            return

        # Rate limit check
        key = (network_name, channel)
        now = time.monotonic()
        last = self._cooldowns.get(key, 0.0)
        if now - last < self.cmd_cfg.cooldown_sec:
            log.debug("Rate limit hit for %s/%s — ignoring %s%s", network_name, channel, trigger, command)
            return

        self._cooldowns[key] = now

        event: SongEvent | None = self.last_event[0]

        if command in ("np", "nowplaying"):
            await self._cmd_np(network_name, channel, event)
        elif command == "next":
            await self._cmd_next(network_name, channel, event)

    async def _cmd_np(self, network_name: str, channel: str, event: SongEvent | None) -> None:
        if event is None:
            await self.irc.send(network_name, channel, "No song information available yet.")
            return

        # Use np_format if set, otherwise fall back to the announce format
        fmt = self.cmd_cfg.np_format or self.ann_cfg.format
        from config import AnnounceConfig
        import dataclasses
        tmp_cfg = dataclasses.replace(self.ann_cfg, format=fmt)
        message = format_message(event, tmp_cfg)
        await self.irc.send(network_name, channel, message)

    async def _cmd_next(self, network_name: str, channel: str, event: SongEvent | None) -> None:
        if event is None or event.playing_next is None:
            await self.irc.send(network_name, channel, "No upcoming song information available.")
            return

        nxt = event.playing_next
        fb = self.ann_cfg.fallbacks
        variables = {
            "artist": nxt.artist or fb.artist,
            "title":  nxt.title  or fb.title,
            "text":   nxt.text   or f"{nxt.artist} - {nxt.title}",
        }
        try:
            message = self.cmd_cfg.next_format.format(**variables)
        except KeyError as exc:
            log.error("Unknown variable in next_format: %s", exc)
            message = f"Next up: {variables['artist']} - {variables['title']}"
        await self.irc.send(network_name, channel, message)

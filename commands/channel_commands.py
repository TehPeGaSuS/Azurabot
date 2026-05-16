"""
commands/channel_commands.py — Public channel commands (!np, !next).

Anyone in a registered channel can trigger these. Responses are
suppressed per channel if the song hasn't changed since the last reply,
so the current song acts as the natural cooldown.
"""

from __future__ import annotations

import logging

from announcer import format_message
from config import CommandsConfig, AnnounceConfig, AzuracastConfig
from webhook import SongEvent, fetch_now_playing

log = logging.getLogger(__name__)


class ChannelCommandHandler:
    def __init__(
        self,
        commands_cfg: CommandsConfig,
        announce_cfg: AnnounceConfig,
        azuracast_cfg: AzuracastConfig,
        irc_manager,       # irc.manager.IRCManager
        last_event: list,  # [SongEvent | None]
        db,                # db.Database
    ) -> None:
        self.cmd_cfg = commands_cfg
        self.ann_cfg = announce_cfg
        self.az_cfg = azuracast_cfg
        self.irc = irc_manager
        self.last_event = last_event
        self.db = db

        # (network_name, channel, command) -> last song_id replied with
        self._last_replied: dict[tuple[str, str, str], str] = {}

    def reload(self, new_cfg, removed_networks: list[str] | None = None) -> None:
        """Swap config in-place and prune state for removed networks."""
        self.cmd_cfg = new_cfg.commands
        self.ann_cfg = new_cfg.announce
        self.az_cfg = new_cfg.azuracast
        if removed_networks:
            for key in list(self._last_replied):
                if key[0] in removed_networks:
                    del self._last_replied[key]

    async def handle(self, network_name: str, channel: str, mask: str, message: str) -> None:
        """Called for every PRIVMSG in a channel. Checks for trigger prefix."""
        message = message.strip()
        trigger = self.cmd_cfg.trigger

        if not message.startswith(trigger):
            return

        command = message[len(trigger):].split()[0].lower()
        if command not in ("np", "nowplaying", "next"):
            return

        # Only respond in channels that are registered in the DB
        row = await self.db.get_channel(network_name, channel)
        if row is None:
            log.debug("Ignoring %s%s in unregistered channel %s/%s", trigger, command, network_name, channel)
            return

        event: SongEvent | None = self.last_event[0]

        if command in ("np", "nowplaying"):
            await self._cmd_np(network_name, channel, event)
        elif command == "next":
            await self._cmd_next(network_name, channel, event)

    async def _get_event(self) -> SongEvent | None:
        """Return last_event if available, otherwise fall back to a live API poll."""
        if self.last_event[0] is not None:
            return self.last_event[0]
        url = self.az_cfg.nowplaying_url
        if not url:
            return None
        log.debug("No cached event — polling AzuraCast API")
        event = await fetch_now_playing(url)
        if event is not None:
            self.last_event[0] = event
        return event

    async def _cmd_np(self, network_name: str, channel: str, event: SongEvent | None) -> None:
        event = await self._get_event()
        if event is None:
            await self.irc.send(network_name, channel, "No song information available yet.")
            return

        key = (network_name, channel, "np")
        if self._last_replied.get(key) == event.song_id:
            log.debug("Suppressing !np in %s/%s — same song_id %s", network_name, channel, event.song_id)
            return
        self._last_replied[key] = event.song_id

        # Use np_format if set, otherwise fall back to the announce format
        fmt = self.cmd_cfg.np_format or self.ann_cfg.format
        from config import AnnounceConfig
        import dataclasses
        tmp_cfg = dataclasses.replace(self.ann_cfg, format=fmt)
        message = format_message(event, tmp_cfg)
        await self.irc.send(network_name, channel, message)

    async def _cmd_next(self, network_name: str, channel: str, event: SongEvent | None) -> None:
        event = await self._get_event()
        if event is None or event.playing_next is None:
            await self.irc.send(network_name, channel, "No upcoming song information available.")
            return

        key = (network_name, channel, "next")
        if self._last_replied.get(key) == event.playing_next.song_id:
            log.debug("Suppressing !next in %s/%s — same song_id %s", network_name, channel, event.playing_next.song_id)
            return
        self._last_replied[key] = event.playing_next.song_id

        nxt = event.playing_next
        fb = self.ann_cfg.fallbacks
        variables = {
            "artist":     nxt.artist or fb.artist,
            "title":      nxt.title  or fb.title,
            "text":       nxt.text   or f"{nxt.artist} - {nxt.title}",
            "radio_name": event.radio_name,
        }
        try:
            message = self.cmd_cfg.next_format.format(**variables)
        except KeyError as exc:
            log.error("Unknown variable in next_format: %s", exc)
            message = f"Next up: {variables['artist']} - {variables['title']}"
        await self.irc.send(network_name, channel, message)

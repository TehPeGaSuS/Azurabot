"""
commands/pm_commands.py — Owner PM command handler.

Auth: the owner does `/msg bot identify <nickname> <password>`.
Sessions are stored in memory, keyed by (network_name, nick!user@host).
This means authenticating on DALnet does NOT grant a session on Libera —
each network requires its own identify. However, once authenticated on any
network, the owner can manage channels/settings across all networks.

After 3 failed attempts from a (network, mask) pair, it is locked out for
10 minutes.

All commands are case-insensitive. Unrecognized commands get a help hint.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone

from config import Config
from db import Database
from webhook import SongEvent

log = logging.getLogger(__name__)

_LOCKOUT_ATTEMPTS = 3
_LOCKOUT_DURATION = 600  # seconds
_INTERVAL_RE = re.compile(r"^(\d+)(m|h)$")


def _valid_mode(mode: str) -> bool:
    if mode in ("live", "hourly"):
        return True
    return bool(_INTERVAL_RE.match(mode))


class PMCommandHandler:
    def __init__(
        self,
        cfg: Config,
        db: Database,
        irc_manager,      # irc.manager.IRCManager
        webhook_server,   # webhook.WebhookServer
        event_queue: asyncio.Queue,
        last_event: list,  # mutable container: [SongEvent | None]
    ) -> None:
        self.cfg = cfg
        self.db = db
        self.irc = irc_manager
        self.webhook = webhook_server
        self.queue = event_queue
        self.last_event = last_event  # shared reference from main
        self.reload_callback = None  # set by main after wiring

        # (network_name, mask) -> True (authenticated)
        self._sessions: dict[tuple[str, str], bool] = {}
        # (network_name, mask) -> (fail_count, lockout_until)
        self._failed: dict[tuple[str, str], tuple[int, float]] = {}

    # ------------------------------------------------------------------ #
    # Entry point — called by IRCClient on every PM
    # ------------------------------------------------------------------ #

    async def handle(self, network_name: str, mask: str, message: str) -> None:
        message = message.strip()
        if not message:
            return

        parts = message.split()
        cmd = parts[0].lower()
        args = parts[1:]

        # Always allow identify and logout without session check
        if cmd == "identify":
            await self._cmd_identify(network_name, mask, args)
            return
        if cmd == "logout":
            await self._cmd_logout(network_name, mask)
            return

        if not self._is_authed(network_name, mask):
            await self._reply(network_name, mask, "Not authenticated. Use: identify <nickname> <password>")
            return

        dispatch = {
            "add":        self._cmd_add,
            "remove":     self._cmd_remove,
            "list":       self._cmd_list,
            "info":       self._cmd_info,
            "set":        self._cmd_set,
            "enable":     self._cmd_enable,
            "disable":    self._cmd_disable,
            "test":       self._cmd_test,
            "status":     self._cmd_status,
            "netstatus":  self._cmd_netstatus,
            "reconnect":  self._cmd_reconnect,
            "reload":     self._cmd_reload,
            "help":       self._cmd_help,
        }

        handler = dispatch.get(cmd)
        if handler:
            await handler(network_name, mask, args)
        else:
            await self._reply(network_name, mask, f"Unknown command '{cmd}'. Say 'help' for a list.")

    # ------------------------------------------------------------------ #
    # Auth
    # ------------------------------------------------------------------ #

    def _is_authed(self, network_name: str, mask: str) -> bool:
        return self._sessions.get((network_name, mask), False)

    def _is_locked(self, network_name: str, mask: str) -> bool:
        key = (network_name, mask)
        entry = self._failed.get(key)
        if not entry:
            return False
        count, until = entry
        if count >= _LOCKOUT_ATTEMPTS and time.time() < until:
            return True
        if time.time() >= until:
            self._failed.pop(key, None)
        return False

    async def _cmd_identify(self, network_name: str, mask: str, args: list[str]) -> None:
        if self._is_locked(network_name, mask):
            await self._reply(network_name, mask, "Too many failed attempts. Try again later.")
            return

        if len(args) < 2:
            await self._reply(network_name, mask, "Usage: identify <nickname> <password>")
            return

        nickname, password = args[0], args[1]
        key = (network_name, mask)
        if nickname == self.cfg.owner.nickname and password == self.cfg.owner.password:
            self._sessions[key] = True
            self._failed.pop(key, None)
            log.info("Owner identified from %s on %s", mask, network_name)
            await self._reply(network_name, mask, "Authenticated. Welcome.")
        else:
            count, _ = self._failed.get(key, (0, 0))
            count += 1
            until = time.time() + _LOCKOUT_DURATION if count >= _LOCKOUT_ATTEMPTS else 0
            self._failed[key] = (count, until)
            remaining = _LOCKOUT_ATTEMPTS - count
            if remaining > 0:
                await self._reply(network_name, mask, f"Wrong credentials. {remaining} attempt(s) left.")
            else:
                await self._reply(network_name, mask, "Too many failed attempts. Locked out for 10 minutes.")
            log.warning("Failed identify attempt from %s on %s (%d/%d)", mask, network_name, count, _LOCKOUT_ATTEMPTS)

    async def _cmd_logout(self, network_name: str, mask: str) -> None:
        if self._sessions.pop((network_name, mask), False):
            await self._reply(network_name, mask, "Logged out.")
        else:
            await self._reply(network_name, mask, "You were not logged in.")

    def clear_session(self, network_name: str, mask: str) -> None:
        """Called externally when a QUIT is observed for this mask on this network."""
        if self._sessions.pop((network_name, mask), False):
            log.info("Session cleared on QUIT: %s on %s", mask, network_name)

    # ------------------------------------------------------------------ #
    # Channel management
    # ------------------------------------------------------------------ #

    async def _cmd_add(self, network_name: str, mask: str, args: list[str]) -> None:
        """add <#channel> [mode]  — mode defaults to 'live'"""
        if not args:
            await self._reply(network_name, mask, "Usage: add <#channel> [live|hourly|Xm|Xh]")
            return
        channel = args[0]
        mode = args[1] if len(args) > 1 else "live"
        if not _valid_mode(mode):
            await self._reply(network_name, mask, f"Invalid mode '{mode}'. Use: live, hourly, Xm, or Xh")
            return
        row_id = await self.db.add_channel(network_name, channel, mode)
        if row_id is None:
            await self._reply(network_name, mask, f"{channel} on {network_name} is already registered.")
        else:
            await self.irc.join_channel(network_name, channel)
            await self._reply(network_name, mask, f"Added {channel} on {network_name} (mode: {mode}).")

    async def _cmd_remove(self, network_name: str, mask: str, args: list[str]) -> None:
        """remove <#channel> [network]"""
        if not args:
            await self._reply(network_name, mask, "Usage: remove <#channel> [network]")
            return
        channel = args[0]
        net = args[1] if len(args) > 1 else network_name
        removed = await self.db.remove_channel(net, channel)
        if removed:
            await self.irc.part_channel(net, channel)
            await self._reply(network_name, mask, f"Removed {channel} from {net}.")
        else:
            await self._reply(network_name, mask, f"{channel} on {net} not found.")

    async def _cmd_list(self, network_name: str, mask: str, args: list[str]) -> None:
        rows = await self.db.get_all_channels()
        if not rows:
            await self._reply(network_name, mask, "No channels registered.")
            return
        await self._reply(network_name, mask, f"{'ID':<4} {'Network':<12} {'Channel':<20} {'Mode':<10} {'Listeners':<10} {'DJ':<6} {'Enabled'}")
        await self._reply(network_name, mask, "-" * 72)
        for row in rows:
            await self._reply(
                network_name, mask,
                f"{row['id']:<4} {row['network_name']:<12} {row['channel']:<20} "
                f"{row['announce_mode']:<10} {'on' if row['show_listeners'] else 'off':<10} "
                f"{'on' if row['show_dj'] else 'off':<6} {'yes' if row['enabled'] else 'no'}"
            )

    async def _cmd_info(self, network_name: str, mask: str, args: list[str]) -> None:
        """info <#channel> [network]"""
        if not args:
            await self._reply(network_name, mask, "Usage: info <#channel> [network]")
            return
        channel = args[0]
        net = args[1] if len(args) > 1 else network_name
        row = await self.db.get_channel(net, channel)
        if not row:
            await self._reply(network_name, mask, f"{channel} on {net} not found.")
            return
        await self._reply(network_name, mask, f"Channel:      {row['channel']}")
        await self._reply(network_name, mask, f"Network:      {row['network_name']}")
        await self._reply(network_name, mask, f"Mode:         {row['announce_mode']}")
        await self._reply(network_name, mask, f"Listeners:    {'on' if row['show_listeners'] else 'off'}")
        await self._reply(network_name, mask, f"DJ name:      {'on' if row['show_dj'] else 'off'}")
        await self._reply(network_name, mask, f"Enabled:      {'yes' if row['enabled'] else 'no'}")
        await self._reply(network_name, mask, f"Last announce:{row['last_announced_at'] or 'never'}")
        await self._reply(network_name, mask, f"Added:        {row['added_at']}")

    # ------------------------------------------------------------------ #
    # Per-channel settings
    # ------------------------------------------------------------------ #

    async def _cmd_set(self, network_name: str, mask: str, args: list[str]) -> None:
        """
        set delay <#channel> <seconds>
        set listeners <#channel> on|off
        set dj <#channel> on|off
        set mode <#channel> live|hourly|Xm|Xh
        """
        if len(args) < 3:
            await self._reply(network_name, mask, "Usage: set <delay|listeners|dj|mode> <#channel> <value> [network]")
            return

        sub, channel, value = args[0].lower(), args[1], args[2]
        net = args[3] if len(args) > 3 else network_name

        row = await self.db.get_channel(net, channel)
        if not row:
            await self._reply(network_name, mask, f"{channel} on {net} not found.")
            return

        if sub == "delay":
            # Legacy: treat as mode Xs/Xm
            await self._reply(network_name, mask, "Use 'set mode' to change announce timing (live, hourly, Xm, Xh).")
            return

        elif sub == "mode":
            if not _valid_mode(value):
                await self._reply(network_name, mask, f"Invalid mode '{value}'. Use: live, hourly, Xm, or Xh")
                return
            await self.db.set_channel_field(net, channel, "announce_mode", value)
            await self._reply(network_name, mask, f"Mode for {channel} on {net} set to '{value}'.")

        elif sub == "listeners":
            if value not in ("on", "off"):
                await self._reply(network_name, mask, "Value must be 'on' or 'off'.")
                return
            await self.db.set_channel_field(net, channel, "show_listeners", 1 if value == "on" else 0)
            await self._reply(network_name, mask, f"Listeners display for {channel} on {net}: {value}.")

        elif sub == "dj":
            if value not in ("on", "off"):
                await self._reply(network_name, mask, "Value must be 'on' or 'off'.")
                return
            await self.db.set_channel_field(net, channel, "show_dj", 1 if value == "on" else 0)
            await self._reply(network_name, mask, f"DJ name display for {channel} on {net}: {value}.")

        else:
            await self._reply(network_name, mask, f"Unknown setting '{sub}'. Use: mode, listeners, dj")

    async def _cmd_enable(self, network_name: str, mask: str, args: list[str]) -> None:
        await self._set_enabled(network_name, mask, args, True)

    async def _cmd_disable(self, network_name: str, mask: str, args: list[str]) -> None:
        await self._set_enabled(network_name, mask, args, False)

    async def _set_enabled(
        self, network_name: str, mask: str, args: list[str], enabled: bool
    ) -> None:
        """enable|disable <#channel> [network]"""
        if not args:
            word = "enable" if enabled else "disable"
            await self._reply(network_name, mask, f"Usage: {word} <#channel> [network]")
            return
        channel = args[0]
        net = args[1] if len(args) > 1 else network_name
        ok = await self.db.set_channel_field(net, channel, "enabled", 1 if enabled else 0)
        word = "Enabled" if enabled else "Disabled"
        if ok:
            await self._reply(network_name, mask, f"{word} {channel} on {net}.")
        else:
            await self._reply(network_name, mask, f"{channel} on {net} not found.")

    # ------------------------------------------------------------------ #
    # Test
    # ------------------------------------------------------------------ #

    async def _cmd_test(self, network_name: str, mask: str, args: list[str]) -> None:
        """test <#channel> [network]"""
        if not args:
            await self._reply(network_name, mask, "Usage: test <#channel> [network]")
            return

        channel = args[0]
        net = args[1] if len(args) > 1 else network_name

        row = await self.db.get_channel(net, channel)
        if not row:
            await self._reply(network_name, mask, f"{channel} on {net} not found.")
            return

        event = self.last_event[0]
        if event is None:
            # Send a dummy event
            from webhook import NextSong
            event = SongEvent(
                song_id="test-000",
                artist="Test Artist",
                title="Test Song",
                text="Test Artist - Test Song",
                listeners=0,
                dj_name="Test DJ",
                radio_name="Test Radio",
                station_url="https://example.com/public/station",
                playing_next=NextSong(artist="Next Artist", title="Next Song", text="Next Artist - Next Song"),
                received_at=datetime.now(timezone.utc),
            )

        from announcer import format_message
        message = format_message(event, self.cfg.announce)
        sent = await self.irc.send(net, channel, message)
        if sent:
            await self._reply(network_name, mask, f"Test announcement sent to {channel} on {net}.")
        else:
            await self._reply(network_name, mask, f"Failed to send — {net} may not be connected.")

    # ------------------------------------------------------------------ #
    # Status / network
    # ------------------------------------------------------------------ #

    async def _cmd_status(self, network_name: str, mask: str, args: list[str]) -> None:
        q_depth = self.queue.qsize()
        last = self.webhook.last_event_at
        last_str = last.strftime("%Y-%m-%d %H:%M:%S UTC") if last else "never"
        await self._reply(network_name, mask, f"Webhook: running | Queue depth: {q_depth} | Last event: {last_str}")

    async def _cmd_netstatus(self, network_name: str, mask: str, args: list[str]) -> None:
        statuses = self.irc.status()
        if not statuses:
            await self._reply(network_name, mask, "No networks configured.")
            return
        for name, connected in statuses.items():
            state = "\x0309connected\x03" if connected else "\x0304disconnected\x03"
            await self._reply(network_name, mask, f"  {name}: {state}")

    async def _cmd_reconnect(self, network_name: str, mask: str, args: list[str]) -> None:
        """reconnect <network>"""
        if not args:
            await self._reply(network_name, mask, "Usage: reconnect <network>")
            return
        net = args[0]
        ok = await self.irc.reconnect(net)
        if ok:
            await self._reply(network_name, mask, f"Reconnecting {net}...")
        else:
            await self._reply(network_name, mask, f"Network '{net}' not found.")

    async def _cmd_reload(self, network_name: str, mask: str, args: list[str]) -> None:
        """reload [--purge]  — Reload config.toml and apply network changes live."""
        purge = "--purge" in args
        if not self.reload_callback:
            await self._reply(network_name, mask, "Reload not available (callback not wired).")
            return
        await self._reply(network_name, mask, "Reloading config...")
        try:
            summary = await self.reload_callback(purge=purge)
        except Exception as exc:
            await self._reply(network_name, mask, f"Reload failed: {exc}")
            return

        parts = []
        if summary["added"]:
            parts.append(f"added: {', '.join(summary['added'])}")
        if summary["removed"]:
            verb = "removed+purged" if purge else "removed"
            parts.append(f"{verb}: {', '.join(summary['removed'])}")
        if summary["restarted"]:
            parts.append(f"restarted: {', '.join(summary['restarted'])}")
        if summary["unchanged"]:
            parts.append(f"unchanged: {', '.join(summary['unchanged'])}")
        await self._reply(network_name, mask, "Done. " + (" | ".join(parts) if parts else "No changes."))

    # ------------------------------------------------------------------ #
    # Help
    # ------------------------------------------------------------------ #

    async def _cmd_help(self, network_name: str, mask: str, args: list[str]) -> None:
        lines = [
            "Available commands:",
            "  identify <nick> <pass>            — Authenticate",
            "  logout                            — End session",
            "  add <#channel> [mode]             — Register channel (mode: live, hourly, Xm, Xh)",
            "  remove <#channel> [network]       — Unregister channel",
            "  list                              — List all channels",
            "  info <#channel> [network]         — Channel details",
            "  set mode <#channel> <mode> [net]  — Change announce mode",
            "  set listeners <#ch> on|off [net]  — Toggle listener count",
            "  set dj <#ch> on|off [net]         — Toggle DJ name",
            "  enable <#channel> [network]       — Resume announcements",
            "  disable <#channel> [network]      — Pause announcements",
            "  test <#channel> [network]         — Send test announcement",
            "  status                            — Webhook + queue status",
            "  netstatus                         — IRC connection status",
            "  reconnect <network>               — Force reconnect a network",
            "  reload [--purge]                  — Reload config, apply network changes live",
        ]
        for line in lines:
            await self._reply(network_name, mask, line)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    async def _reply(self, network_name: str, mask: str, message: str) -> None:
        nick = mask.split("!")[0]
        await self.irc.send(network_name, nick, message)

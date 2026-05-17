"""
irc/client.py — Pure asyncio IRC client. No external IRC library.

Handles:
  - TLS and plain connections
  - SASL PLAIN, NickServ IDENTIFY, on-connect commands, no auth
  - PING/PONG keepalive
  - Automatic reconnect with exponential backoff
  - Incoming PM routing to the command handler
  - QUIT detection for session clearing
"""

from __future__ import annotations

import asyncio
import base64
import logging
import ssl
from typing import Callable

log = logging.getLogger(__name__)

_BACKOFF_START = 10
_BACKOFF_MAX = 300
_ENCODING = "utf-8"
_TIMEOUT = 300        # seconds — drop connection if no data received
_PING_INTERVAL = 90   # send client PING every N seconds to keep connection alive


class IRCClient:
    def __init__(
        self,
        network_cfg,
        pm_handler: Callable,             # async (network_name, mask, message)
        session_clear_handler: Callable,  # async (network_name, mask)
        on_connected_callback: Callable | None = None,  # async (network_name)
        channel_handler: Callable | None = None,  # async (network_name, channel, mask, message)
    ) -> None:
        self.cfg = network_cfg
        self.pm_handler = pm_handler
        self.session_clear_handler = session_clear_handler
        self.on_connected_callback = on_connected_callback
        self.channel_handler = channel_handler

        self.nick = network_cfg.nick
        self._running = False
        self._connected = False
        self._writer: asyncio.StreamWriter | None = None
        self._backoff = _BACKOFF_START
        self._sasl_requested = False
        self._registered = False
        self._nick_taken_handled = False  # only attempt recovery once per connect

    @property
    def name(self) -> str:
        return self.cfg.name

    @property
    def connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        self._running = True
        await self._connect_loop()

    async def stop(self) -> None:
        self._running = False
        await self._raw("QUIT :Shutting down")
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass

    async def send(self, target: str, message: str) -> bool:
        if not self._connected:
            return False
        max_len = 400
        while message:
            chunk, message = message[:max_len], message[max_len:]
            await self._raw(f"PRIVMSG {target} :{chunk}")
        return True

    async def join(self, channel: str) -> None:
        if self._connected:
            await self._raw(f"JOIN {channel}")

    async def part(self, channel: str) -> None:
        if self._connected:
            await self._raw(f"PART {channel}")

    # ------------------------------------------------------------------ #
    # Connection loop
    # ------------------------------------------------------------------ #

    async def _connect_loop(self) -> None:
        while self._running:
            try:
                await self._connect()
            except Exception as exc:
                log.error("[%s] Connection error: %s", self.name, exc)
            finally:
                self._connected = False
                if self._writer:
                    self._writer.close()
                    self._writer = None

            if not self._running:
                break

            log.info("[%s] Reconnecting in %ds", self.name, self._backoff)
            await asyncio.sleep(self._backoff)
            self._backoff = min(self._backoff * 2, _BACKOFF_MAX)

    async def _connect(self) -> None:
        cfg = self.cfg
        log.info("[%s] Connecting to %s:%d (tls=%s)", self.name, cfg.host, cfg.port, cfg.tls)

        ssl_ctx: ssl.SSLContext | bool = False
        if cfg.tls:
            ssl_ctx = ssl.create_default_context()

        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(cfg.host, cfg.port, ssl=ssl_ctx),
            timeout=30,
        )
        self._writer = writer
        self._registered = False
        self._sasl_requested = False
        self._nick_taken_handled = False

        # Enable TCP keepalives so NAT/firewall middleboxes don't silently
        # drop idle connections (common with ircd-hybrid networks).
        sock = writer.get_extra_info("socket")
        if sock is not None:
            import socket as _socket
            sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_KEEPALIVE, 1)

        log.info("[%s] TCP connected", self.name)

        await self._begin_registration()
        ping_task = asyncio.create_task(self._ping_loop(cfg.host), name=f"ping-{self.name}")
        try:
            await self._read_loop(reader)
        finally:
            ping_task.cancel()
            try:
                await ping_task
            except asyncio.CancelledError:
                pass

    async def _begin_registration(self) -> None:
        cfg = self.cfg
        if cfg.auth_method == "sasl":
            await self._raw("CAP REQ :sasl")
        else:
            await self._raw(f"NICK {cfg.nick}")
            await self._raw(f"USER {cfg.nick} 0 * :{cfg.nick}")

    # ------------------------------------------------------------------ #
    # Read loop & line dispatch
    # ------------------------------------------------------------------ #

    async def _ping_loop(self, host: str) -> None:
        """Send a PING to the server every _PING_INTERVAL seconds.
        Keeps ircd-hybrid (and other IRCds) from timing out idle connections.
        We don't track PONG replies — the read loop's _TIMEOUT handles dead connections.
        """
        await asyncio.sleep(_PING_INTERVAL)
        while True:
            try:
                await self._raw(f"PING :{host}")
            except Exception:
                return
            await asyncio.sleep(_PING_INTERVAL)

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        while True:
            try:
                data = await asyncio.wait_for(reader.readline(), timeout=_TIMEOUT)
            except asyncio.TimeoutError:
                log.warning("[%s] Read timeout — dropping connection", self.name)
                return
            if not data:
                log.warning("[%s] Connection closed by server", self.name)
                return
            line = data.decode(_ENCODING, errors="replace").rstrip("\r\n")
            if line:
                await self._handle_line(line)

    async def _handle_line(self, line: str) -> None:
        log.debug("[%s] << %s", self.name, line)

        if line.startswith("PING"):
            # Mirror the server's format: "PING :token" → "PONG :token"
            # but "PING token" (ircd-hybrid style) → "PONG token" (no colon)
            rest = line[5:]  # everything after "PING "
            await self._raw(f"PONG {rest}")
            return

        parts = line.split()
        if len(parts) < 2:
            return

        if parts[0].startswith(":"):
            prefix = parts[0][1:]
            command = parts[1].upper()
            params = parts[2:]
        else:
            prefix = ""
            command = parts[0].upper()
            params = parts[1:]

        if command == "CAP":
            await self._handle_cap(params)

        elif command == "AUTHENTICATE":
            await self._handle_authenticate(params)

        elif command == "903":
            log.info("[%s] SASL authentication successful", self.name)
            await self._raw("CAP END")
            await self._raw(f"NICK {self.cfg.nick}")
            await self._raw(f"USER {self.cfg.nick} 0 * :{self.cfg.nick}")

        elif command in ("904", "905"):
            log.error("[%s] SASL authentication failed (%s)", self.name, command)
            await self._raw("CAP END")

        elif command == "001":
            self._connected = True
            self._registered = True
            self._backoff = _BACKOFF_START
            log.info("[%s] Registered as %s", self.name, self.cfg.nick)
            await self._post_connect_auth()

        elif command in ("433", "437"):
            reason = "Nick in use" if command == "433" else "Nick unavailable"
            if self._nick_taken_handled:
                log.warning("[%s] %s and recovery already attempted — waiting for reconnect", self.name, reason)
                return
            self._nick_taken_handled = True
            cmds = self.cfg.nick_taken_commands
            if cmds:
                log.warning("[%s] %s — running nick_taken_commands", self.name, reason)
                for cmd in cmds:
                    await self._raw(cmd)
            else:
                # Fallback: append _ to nick and try again
                new_nick = self.cfg.nick + "_"
                log.warning("[%s] %s, trying %s (no nick_taken_commands configured)", self.name, reason, new_nick)
                self.nick = new_nick
                await self._raw(f"NICK {new_nick}")

        elif command == "PRIVMSG" and prefix:
            await self._handle_privmsg(prefix, params)

        elif command == "QUIT" and prefix:
            mask = prefix
            if "!" in mask:
                await self.session_clear_handler(self.name, mask)

    # ------------------------------------------------------------------ #
    # CAP / SASL
    # ------------------------------------------------------------------ #

    async def _handle_cap(self, params: list[str]) -> None:
        if len(params) < 2:
            return
        subcommand = params[1].upper()
        caps = " ".join(params[2:]).lstrip(":")

        if subcommand == "ACK" and "sasl" in caps:
            log.debug("[%s] CAP ACK sasl", self.name)
            await self._raw("AUTHENTICATE PLAIN")
            self._sasl_requested = True
        elif subcommand == "NAK":
            log.warning("[%s] CAP NAK: %s — continuing without SASL", self.name, caps)
            await self._raw("CAP END")
            await self._raw(f"NICK {self.cfg.nick}")
            await self._raw(f"USER {self.cfg.nick} 0 * :{self.cfg.nick}")

    async def _handle_authenticate(self, params: list[str]) -> None:
        if params and params[0] == "+":
            cfg = self.cfg
            payload = f"\0{cfg.sasl_user}\0{cfg.sasl_pass}"
            encoded = base64.b64encode(payload.encode(_ENCODING)).decode(_ENCODING)
            await self._raw(f"AUTHENTICATE {encoded}")

    # ------------------------------------------------------------------ #
    # Post-connect auth
    # ------------------------------------------------------------------ #

    async def _post_connect_auth(self) -> None:
        cfg = self.cfg

        if cfg.auth_method == "nickserv":
            await asyncio.sleep(1)
            await self._raw(f"PRIVMSG NickServ :IDENTIFY {cfg.nickserv_pass}")
            log.debug("[%s] Sent NickServ IDENTIFY", self.name)

        elif cfg.auth_method == "on_connect":
            await asyncio.sleep(cfg.on_connect_delay_sec)
            for cmd in cfg.on_connect_commands:
                await self._raw(cmd)
                log.debug("[%s] on-connect: %s", self.name, cmd)
                await asyncio.sleep(0.5)

        if cfg.home_channel:
            await self._raw(f"JOIN {cfg.home_channel}")
            log.info("[%s] Joined home channel %s", self.name, cfg.home_channel)

        if self.on_connected_callback:
            await self.on_connected_callback(self.name)

    # ------------------------------------------------------------------ #
    # PRIVMSG handler
    # ------------------------------------------------------------------ #

    async def _handle_privmsg(self, mask: str, params: list[str]) -> None:
        if not params:
            return
        target = params[0]
        text = " ".join(params[1:]).lstrip(":")
        if target.lower() == self.nick.lower():
            # Private message to the bot
            await self.pm_handler(self.name, mask, text)
        elif target.startswith(("#", "&", "+", "!")) and self.channel_handler:
            # Channel message
            await self.channel_handler(self.name, target, mask, text)

    # ------------------------------------------------------------------ #
    # Raw send
    # ------------------------------------------------------------------ #

    async def _raw(self, line: str) -> None:
        if self._writer is None:
            return
        log.debug("[%s] >> %s", self.name, line)
        try:
            self._writer.write((line + "\r\n").encode(_ENCODING))
            await self._writer.drain()
        except Exception as exc:
            log.error("[%s] Send error: %s", self.name, exc)

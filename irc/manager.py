"""
irc/manager.py — Manages all IRCClient instances (one per network).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

from config import Config
from irc.client import IRCClient

log = logging.getLogger(__name__)


class IRCManager:
    def __init__(self, cfg: Config, pm_handler: Callable, session_clear_handler: Callable, db, channel_handler: Callable | None = None) -> None:
        self._clients: dict[str, IRCClient] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._cfg = cfg
        self._pm_handler = pm_handler
        self._session_clear_handler = session_clear_handler
        self._channel_handler = channel_handler
        self._db = db

    async def _on_network_connected(self, network_name: str) -> None:
        """Auto-register the home channel in DB on every connect/reconnect if not already there."""
        net = self._cfg.get_network(network_name)
        if not net or not net.home_channel:
            return
        row = await self._db.get_channel(network_name, net.home_channel)
        if row is None:
            await self._db.add_channel(network_name, net.home_channel, announce_mode="live")
            log.info("Auto-registered home channel %s for network %s", net.home_channel, network_name)

    async def start(self) -> None:
        for net in self._cfg.networks:
            if net.name in self._clients:
                continue
            client = IRCClient(
                net,
                self._pm_handler,
                self._session_clear_handler,
                on_connected_callback=self._on_network_connected,
                channel_handler=self._channel_handler,
            )
            self._clients[net.name] = client
            # Fire each client as a background task — start() runs the
            # connect/reconnect loop forever and must not be awaited here.
            task = asyncio.create_task(client.start(), name=f"irc-{net.name}")
            self._tasks[net.name] = task
            log.info("Started IRC client for network: %s", net.name)

    async def stop(self) -> None:
        for name, client in self._clients.items():
            await client.stop()
        for task in self._tasks.values():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def send(self, network_name: str, channel: str, message: str) -> bool:
        client = self._clients.get(network_name)
        if not client:
            log.warning("No client for network '%s'", network_name)
            return False
        return await client.send(channel, message)

    async def join_channel(self, network_name: str, channel: str) -> None:
        client = self._clients.get(network_name)
        if client:
            await client.join(channel)

    async def part_channel(self, network_name: str, channel: str) -> None:
        client = self._clients.get(network_name)
        if client:
            await client.part(channel)

    async def reconnect(self, network_name: str) -> bool:
        client = self._clients.get(network_name)
        if not client:
            return False
        task = self._tasks.get(network_name)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await client.stop()
        await asyncio.sleep(1)
        new_task = asyncio.create_task(client.start(), name=f"irc-{network_name}")
        self._tasks[network_name] = new_task
        return True

    def status(self) -> dict[str, bool]:
        return {name: c.connected for name, c in self._clients.items()}

    def get_client(self, network_name: str) -> IRCClient | None:
        return self._clients.get(network_name)

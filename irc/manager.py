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
        """Auto-register the home channel in DB on every connect/reconnect if not already there,
        then re-join all enabled channels for this network."""
        net = self._cfg.get_network(network_name)
        if net and net.home_channel:
            row = await self._db.get_channel(network_name, net.home_channel)
            if row is None:
                await self._db.add_channel(network_name, net.home_channel, announce_mode="live")
                log.info("Auto-registered home channel %s for network %s", net.home_channel, network_name)

        client = self._clients.get(network_name)
        if not client:
            return
        channels = await self._db.get_enabled_channels()
        for row in channels:
            if row["network_name"] == network_name:
                await client.join(row["channel"])
                log.info("Rejoined %s on %s", row["channel"], network_name)

    async def start(self) -> None:
        for net in self._cfg.networks:
            if net.name not in self._clients:
                await self._start_network(net)
                log.info("Started IRC client for network: %s", net.name)

    async def stop(self) -> None:
        for name in list(self._clients.keys()):
            await self._stop_network(name)

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
        net = self._cfg.get_network(network_name)
        if not net or network_name not in self._clients:
            return False
        await self._stop_network(network_name)
        await asyncio.sleep(1)
        await self._start_network(net)
        return True

    async def reload(self, new_cfg, db, purge: bool = False) -> dict:
        """
        Diff old vs new network list and apply changes live.
        Returns a summary dict with keys 'added', 'removed', 'restarted', 'unchanged'.
        """
        old_names = set(self._clients.keys())
        new_map = {n.name: n for n in new_cfg.networks}
        new_names = set(new_map.keys())

        added = new_names - old_names
        removed = old_names - new_names
        kept = old_names & new_names

        restarted = set()
        unchanged = set()

        # Determine which kept networks have actually changed
        for name in kept:
            old_net = self._cfg.get_network(name)
            new_net = new_map[name]
            # Compare all fields that affect the connection
            if (old_net.host != new_net.host or old_net.port != new_net.port or
                    old_net.tls != new_net.tls or old_net.nick != new_net.nick or
                    old_net.auth_method != new_net.auth_method or
                    old_net.sasl_user != new_net.sasl_user or
                    old_net.sasl_pass != new_net.sasl_pass or
                    old_net.nickserv_pass != new_net.nickserv_pass or
                    old_net.on_connect_commands != new_net.on_connect_commands or
                    old_net.home_channel != new_net.home_channel):
                restarted.add(name)
            else:
                unchanged.add(name)

        # Stop removed networks
        for name in removed:
            await self._stop_network(name)
            if purge:
                channels = await db.get_network_channels(name)
                for row in channels:
                    await db.remove_channel(name, row["channel"])
                log.info("Purged %d channel(s) for removed network %s", len(channels), name)
            log.info("Removed network: %s", name)

        # Restart changed networks
        for name in restarted:
            await self._stop_network(name)
            await self._start_network(new_map[name])
            log.info("Restarted network (config changed): %s", name)

        # Start new networks
        for name in added:
            await self._start_network(new_map[name])
            log.info("Started new network: %s", name)

        self._cfg = new_cfg
        return {
            "added": sorted(added),
            "removed": sorted(removed),
            "restarted": sorted(restarted),
            "unchanged": sorted(unchanged),
        }

    async def _start_network(self, net_cfg) -> None:
        client = IRCClient(
            net_cfg,
            self._pm_handler,
            self._session_clear_handler,
            on_connected_callback=self._on_network_connected,
            channel_handler=self._channel_handler,
        )
        self._clients[net_cfg.name] = client
        task = asyncio.create_task(client.start(), name=f"irc-{net_cfg.name}")
        self._tasks[net_cfg.name] = task

    async def _stop_network(self, name: str) -> None:
        client = self._clients.pop(name, None)
        task = self._tasks.pop(name, None)
        if client:
            await client.stop()
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def status(self) -> dict[str, bool]:
        return {name: c.connected for name, c in self._clients.items()}

    def get_client(self, network_name: str) -> IRCClient | None:
        return self._clients.get(network_name)

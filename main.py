"""
main.py — Entry point. Wires together the webhook server, IRC manager,
dispatcher, and PM command handler, then runs the asyncio event loop.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path

import config as cfg_module
from db import Database
from dispatcher import Dispatcher
from irc.manager import IRCManager
from commands.pm_commands import PMCommandHandler
from webhook import WebhookServer, SongEvent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


async def main() -> None:
    config_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("config.toml")
    log.info("Loading config from %s", config_path)
    cfg = cfg_module.load(config_path)

    # ── Database ──────────────────────────────────────────────────────────
    db = Database(cfg.database.path)
    await db.connect()

    # Warn about channels whose network no longer exists in config
    known_networks = {n.name for n in cfg.networks}
    all_channels = await db.get_all_channels()
    for row in all_channels:
        if row["network_name"] not in known_networks:
            log.warning(
                "Channel %s references unknown network '%s' — skipped at runtime",
                row["channel"], row["network_name"],
            )

    # ── Shared state ──────────────────────────────────────────────────────
    event_queue: asyncio.Queue[SongEvent] = asyncio.Queue()
    last_event: list[SongEvent | None] = [None]

    # ── Webhook server ────────────────────────────────────────────────────
    webhook = WebhookServer(
        queue=event_queue,
        secret=cfg.webhook.secret,
        host=cfg.webhook.host,
        port=cfg.webhook.port,
    )

    # ── Forward declarations for circular wiring ──────────────────────────
    pm_commands: PMCommandHandler | None = None

    async def pm_handler(network_name: str, mask: str, message: str) -> None:
        if pm_commands:
            await pm_commands.handle(network_name, mask, message)

    async def session_clear_handler(network_name: str, mask: str) -> None:
        if pm_commands:
            pm_commands.clear_session(network_name, mask)

    # ── IRC manager ───────────────────────────────────────────────────────
    irc_manager = IRCManager(cfg, pm_handler, session_clear_handler, db)

    # ── PM command handler ────────────────────────────────────────────────
    pm_commands = PMCommandHandler(
        cfg=cfg,
        db=db,
        irc_manager=irc_manager,
        webhook_server=webhook,
        event_queue=event_queue,
        last_event=last_event,
    )

    # ── Dispatcher ────────────────────────────────────────────────────────
    dispatcher = Dispatcher(
        queue=event_queue,
        db=db,
        announce_cfg=cfg.announce,
        irc_manager=irc_manager,
    )

    # Tap the queue to keep last_event updated
    original_put = event_queue.put

    async def tracking_put(item: SongEvent) -> None:
        last_event[0] = item
        await original_put(item)

    event_queue.put = tracking_put  # type: ignore[method-assign]

    # ── Start everything ──────────────────────────────────────────────────
    log.info("Starting webhook server...")
    await webhook.start()

    log.info("Starting IRC clients...")
    await irc_manager.start()  # fires each client as a background task

    log.info("Starting dispatcher...")
    dispatcher.start()

    log.info("Bot running. Press Ctrl+C to stop.")

    # ── Shutdown handling ─────────────────────────────────────────────────
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _signal_handler() -> None:
        log.info("Shutdown signal received")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    await stop_event.wait()

    log.info("Shutting down...")
    await dispatcher.stop()
    await irc_manager.stop()
    await webhook.stop()
    await db.close()
    log.info("Goodbye.")


if __name__ == "__main__":
    asyncio.run(main())

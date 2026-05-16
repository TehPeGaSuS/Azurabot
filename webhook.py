"""
webhook.py — aiohttp server that receives AzuraCast webhook POSTs.
Validates the secret, parses the payload, and enqueues a SongEvent.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from aiohttp import web

log = logging.getLogger(__name__)


@dataclass
class SongEvent:
    song_id: str
    artist: str
    title: str
    text: str
    listeners: int
    dj_name: str
    radio_name: str
    station_url: str
    received_at: datetime


class WebhookServer:
    def __init__(
        self,
        queue: asyncio.Queue,
        secret: str,
        host: str = "0.0.0.0",
        port: int = 8080,
    ) -> None:
        self.queue = queue
        self.secret = secret
        self.host = host
        self.port = port
        self._app = web.Application()
        self._app.router.add_post("/webhook", self._handle)
        self._runner: web.AppRunner | None = None
        self.last_event_at: datetime | None = None

    async def start(self) -> None:
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        log.info("Webhook server listening on %s:%d", self.host, self.port)

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()

    async def _handle(self, request: web.Request) -> web.Response:
        # Validate secret via query param or X-Webhook-Secret header
        secret = request.query.get("secret") or request.headers.get("X-Webhook-Secret", "")
        if self.secret and secret != self.secret:
            log.warning("Webhook rejected: invalid secret from %s", request.remote)
            return web.Response(status=403, text="Forbidden")

        try:
            payload = await request.json()
        except Exception as exc:
            log.warning("Webhook rejected: invalid JSON — %s", exc)
            return web.Response(status=400, text="Bad Request")

        event = _parse(payload)
        if event is None:
            log.warning("Webhook rejected: could not parse payload")
            return web.Response(status=422, text="Unprocessable")

        self.last_event_at = event.received_at
        await self.queue.put(event)
        log.debug("Enqueued song event: %s - %s", event.artist, event.title)
        return web.Response(status=200, text="OK")


def _parse(payload: list | dict) -> SongEvent | None:
    """Parse AzuraCast now-playing payload.

    AzuraCast webhooks wrap the data in:
      {"np": {"App\\Entity\\Api\\NowPlaying\\NowPlaying": { ...data... }}}

    The now-playing API returns either a plain object or an array.
    We handle all three forms.
    """
    try:
        # Unwrap webhook envelope: {"np": {"App\\...": { data }}}
        if isinstance(payload, dict) and "np" in payload:
            inner = payload["np"]
            # inner is {"App\\Entity\\...": { data }}
            data = next(iter(inner.values()))
        elif isinstance(payload, list):
            data = payload[0]
        else:
            data = payload

        now_playing = data["now_playing"]
        song = now_playing["song"]
        live = data.get("live", {})
        listeners = data.get("listeners", {})

        dj_name = ""
        if live.get("is_live"):
            dj_name = live.get("streamer_name", "")
        if not dj_name:
            dj_name = now_playing.get("streamer", "")

        return SongEvent(
            song_id=song["id"],
            artist=song.get("artist", ""),
            title=song.get("title", ""),
            text=song.get("text", ""),
            listeners=listeners.get("current", 0),
            dj_name=dj_name,
            radio_name=data["station"]["name"],
            station_url=data["station"].get("public_player_url", ""),
            received_at=datetime.now(timezone.utc),
        )
    except (KeyError, IndexError, TypeError) as exc:
        log.error("Failed to parse webhook payload: %s", exc)
        return None

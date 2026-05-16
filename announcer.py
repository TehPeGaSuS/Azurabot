"""
announcer.py — Formats SongEvents into IRC messages using the config
format string and sends them to channels.
"""

from __future__ import annotations

import logging

from config import AnnounceConfig
from webhook import SongEvent

log = logging.getLogger(__name__)


def format_message(event: SongEvent, cfg: AnnounceConfig) -> str:
    """Render the announce format string, applying fallbacks for empty fields."""
    fb = cfg.fallbacks

    variables = {
        "radio_name":  event.radio_name or fb.__dict__.get("radio_name", "Radio"),
        "artist":      event.artist     or fb.artist,
        "title":       event.title      or fb.title,
        "text":        event.text       or f"{event.artist} - {event.title}",
        "listeners":   str(event.listeners),
        "dj_name":     event.dj_name    or fb.dj_name,
        "song_id":     event.song_id,
        "station_url": event.station_url,
    }

    try:
        return cfg.format.format(**variables)
    except KeyError as exc:
        log.error("Unknown variable in announce format: %s", exc)
        # Return a safe fallback
        return f"Now Playing: {variables['artist']} - {variables['title']}"

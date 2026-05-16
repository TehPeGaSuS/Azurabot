"""
config.py — Load and validate config.toml into dataclasses.
Networks are always sourced from config; never from the DB.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


AuthMethod = Literal["sasl", "nickserv", "on_connect", "none"]


@dataclass
class WebhookConfig:
    host: str = "0.0.0.0"
    port: int = 8080
    secret: str = ""


@dataclass
class DatabaseConfig:
    path: str = "bot.db"


@dataclass
class OwnerConfig:
    nickname: str = ""
    password: str = ""


@dataclass
class AnnounceFallbacks:
    dj_name: str = "Auto DJ"
    artist: str = "Unknown Artist"
    title: str = "Unknown Title"


@dataclass
class AnnounceConfig:
    format: str = "\x02[{radio_name}]\x02 Now Playing: \x02{artist} - {title}\x02 | {listeners} listeners | DJ: {dj_name}"
    dedup_window_sec: int = 60
    prune_interval_sec: int = 3600
    prune_older_than_sec: int = 300
    fallbacks: AnnounceFallbacks = field(default_factory=AnnounceFallbacks)


@dataclass
class CommandsConfig:
    trigger: str = "!"
    cooldown_sec: int = 60
    np_format: str = ""   # defaults to announce format if empty
    next_format: str = "Next up: \x02{artist} - {title}\x02"



    name: str
    host: str
    port: int
    tls: bool
    nick: str
    auth_method: AuthMethod

    # SASL
    sasl_user: str = ""
    sasl_pass: str = ""

    # NickServ
    nickserv_pass: str = ""

    # On-connect
    on_connect_commands: list[str] = field(default_factory=list)
    on_connect_delay_sec: float = 2.0

    # Home channel — joined automatically on every connect
    home_channel: str = ""


@dataclass
class Config:
    webhook: WebhookConfig
    database: DatabaseConfig
    owner: OwnerConfig
    announce: AnnounceConfig
    commands: CommandsConfig
    networks: list[NetworkConfig]

    def get_network(self, name: str) -> NetworkConfig | None:
        for net in self.networks:
            if net.name == name:
                return net
        return None


def _unescape(s: str) -> str:
    """
    Decode \\xNN escape sequences in a string the way Python would.
    This lets config.toml use \\x02, \\x03, etc. for mIRC formatting codes
    without TOML choking on them (TOML doesn't support \\x escapes natively).
    Use TOML literal strings (single quotes) in config so TOML passes the
    backslashes through verbatim, then this function resolves them.
    """
    return s.encode("raw_unicode_escape").decode("unicode_escape")


def load(path: str | Path = "config.toml") -> Config:
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    webhook = WebhookConfig(**raw.get("webhook", {}))
    database = DatabaseConfig(**raw.get("database", {}))
    owner = OwnerConfig(**raw.get("owner", {}))

    announce_raw = raw.get("announce", {})
    fallbacks_raw = announce_raw.pop("fallbacks", {})
    fallbacks = AnnounceFallbacks(**fallbacks_raw)

    # Unescape \xNN sequences in the format string so mIRC codes work.
    # config.toml must use single-quoted (literal) strings for the format:
    #   format = '\x02[{radio_name}]\x02 Now Playing: \x02{artist} - {title}\x02'
    if "format" in announce_raw:
        announce_raw["format"] = _unescape(announce_raw["format"])

    announce = AnnounceConfig(**announce_raw, fallbacks=fallbacks)

    commands_raw = raw.get("commands", {})
    if "next_format" in commands_raw:
        commands_raw["next_format"] = _unescape(commands_raw["next_format"])
    if "np_format" in commands_raw:
        commands_raw["np_format"] = _unescape(commands_raw["np_format"])
    commands = CommandsConfig(**commands_raw)

    networks = []
    for n in raw.get("networks", []):
        networks.append(NetworkConfig(
            name=n["name"],
            host=n["host"],
            port=n.get("port", 6667),
            tls=n.get("tls", False),
            nick=n["nick"],
            auth_method=n.get("auth_method", "none"),
            sasl_user=n.get("sasl_user", ""),
            sasl_pass=n.get("sasl_pass", ""),
            nickserv_pass=n.get("nickserv_pass", ""),
            on_connect_commands=n.get("on_connect_commands", []),
            on_connect_delay_sec=n.get("on_connect_delay_sec", 2.0),
            home_channel=n.get("home_channel", ""),
        ))

    if not networks:
        raise ValueError("No networks defined in config.toml")

    if not owner.nickname or not owner.password:
        raise ValueError("[owner] nickname and password must be set in config.toml")

    return Config(
        webhook=webhook,
        database=database,
        owner=owner,
        announce=announce,
        commands=commands,
        networks=networks,
    )

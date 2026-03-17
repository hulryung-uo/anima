"""Configuration loader — reads config.yaml and provides typed access."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 2593


@dataclass
class AccountConfig:
    username: str = "admin"
    password: str = "admin"


@dataclass
class CharacterConfig:
    name: str = "Anima"
    template: str = "random"
    city_index: int = 3


@dataclass
class ClientConfig:
    version: str = "7.0.102.3"
    connection_timeout: float = 10.0


@dataclass
class MovementConfig:
    walk_delay_ms: int = 400
    run_delay_ms: int = 200
    turn_delay_ms: int = 100


@dataclass
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    account: AccountConfig = field(default_factory=AccountConfig)
    character: CharacterConfig = field(default_factory=CharacterConfig)
    client: ClientConfig = field(default_factory=ClientConfig)
    movement: MovementConfig = field(default_factory=MovementConfig)


def load_config(path: str | Path | None = None) -> Config:
    """Load config from YAML file. Falls back to defaults if not found."""
    if path is None:
        path = Path(__file__).parent.parent / "config.yaml"
    else:
        path = Path(path)

    cfg = Config()

    if not path.exists():
        return cfg

    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    if "server" in raw:
        for k, v in raw["server"].items():
            if hasattr(cfg.server, k):
                setattr(cfg.server, k, v)

    if "account" in raw:
        for k, v in raw["account"].items():
            if hasattr(cfg.account, k):
                setattr(cfg.account, k, v)

    if "character" in raw:
        for k, v in raw["character"].items():
            if hasattr(cfg.character, k):
                setattr(cfg.character, k, v)

    if "client" in raw:
        for k, v in raw["client"].items():
            if hasattr(cfg.client, k):
                setattr(cfg.client, k, v)

    if "movement" in raw:
        for k, v in raw["movement"].items():
            if hasattr(cfg.movement, k):
                setattr(cfg.movement, k, v)

    return cfg

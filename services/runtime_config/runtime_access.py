"""Runtime configuration access helpers.

Business services use this module when they need runtime-config metadata or a
refresh hook, instead of depending on the global ``Config`` object directly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from core.config import Config


@dataclass(frozen=True, slots=True)
class RuntimeConfigRefreshPolicy:
    enabled: bool

    @classmethod
    def from_config(cls, config: Any | None = None) -> RuntimeConfigRefreshPolicy:
        config = config or Config
        return cls(enabled=bool(config.server.ENABLE_RUNTIME_CONFIG))


def get_runtime_config_meta(key: str) -> dict[str, Any]:
    return dict(Config.get_meta(key))


def get_runtime_config_source(key: str) -> str:
    meta = get_runtime_config_meta(key)
    source = meta.get("source")
    if source:
        return str(source)
    return "env" if os.getenv(key) is not None else "default"


def format_runtime_meta_time(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    return str(value)


async def reload_runtime_config() -> None:
    await Config.load_from_db()

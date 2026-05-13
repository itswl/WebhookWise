"""Runtime mode helpers.

Keep deployment-shape checks in one place so API code can choose between the
full Redis/TaskIQ topology and the single-process lite topology.
"""

from __future__ import annotations

from typing import Any


def current_run_mode(config: Any | None = None) -> str:
    if config is None:
        from core.config import Config

        config = Config
    return str(getattr(config.server, "RUN_MODE", "api") or "api").strip().lower()


def is_lite_mode(config: Any | None = None) -> bool:
    return current_run_mode(config) == "lite"


def uses_taskiq_broker(config: Any | None = None) -> bool:
    return not is_lite_mode(config)


def uses_redis_runtime(config: Any | None = None) -> bool:
    return not is_lite_mode(config)

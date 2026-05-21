"""Shared startup and shutdown helpers for API and worker processes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from adapters.ecosystem_adapters import initialize_adapters
from core.config import UnifiedConfigManager
from core.http_client import close_http_client, get_http_client
from core.logger import stop_log_listener
from core.redis_client import dispose_redis, init_redis
from db.session import dispose_engine, init_engine, test_db_connection
from services.analysis.ai_analyzer import initialize_openai_client, reset_openai_client


@dataclass(frozen=True, slots=True)
class RuntimeServices:
    http_client: httpx.AsyncClient


async def check_database_ready() -> bool:
    await init_engine()
    return await test_db_connection()


async def start_runtime_services(
    config: UnifiedConfigManager,
    *,
    broker: Any | None = None,
    start_broker: bool = False,
    initialize_logger: Callable[[], object] | None = None,
    initialize_observability: Callable[[], None] | None = None,
    initialize_redis_client: bool = False,
    initialize_adapter_registry: bool = True,
    initialize_ai_client: bool = False,
) -> RuntimeServices:
    if initialize_logger is not None:
        initialize_logger()
    if initialize_observability is not None:
        initialize_observability()

    if initialize_adapter_registry:
        initialize_adapters()

    http_client = get_http_client()
    await init_engine()
    if initialize_redis_client:
        init_redis()

    if config.server.ENABLE_RUNTIME_CONFIG:
        await config.load_from_db()
        await config.start_subscriber()

    if initialize_ai_client and config.ai.ENABLE_AI_ANALYSIS and config.ai.OPENAI_API_KEY:
        await initialize_openai_client(http_client=http_client)

    if start_broker and broker is not None:
        await broker.startup()

    return RuntimeServices(http_client=http_client)


async def stop_runtime_services(
    config: UnifiedConfigManager,
    *,
    broker: Any | None = None,
    stop_broker: bool = False,
    reset_ai_client: bool = False,
    dispose_redis_client: bool = True,
    shutdown_observability: Callable[[], None] | None = None,
    stop_logger: bool = False,
) -> None:
    await config.stop_subscriber()

    if stop_broker and broker is not None:
        await broker.shutdown()

    await dispose_engine()
    if dispose_redis_client:
        await dispose_redis()
    if reset_ai_client:
        await reset_openai_client()
    await close_http_client()

    if shutdown_observability is not None:
        shutdown_observability()
    if stop_logger:
        stop_log_listener()

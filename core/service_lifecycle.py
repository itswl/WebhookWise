"""Shared startup and shutdown helpers for API and worker processes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from core.app_context import (
    AppContext,
    get_default_app_context,
    get_or_create_default_app_context,
    init_default_app_context,
    set_default_app_context,
)
from core.config import AppConfig
from core.logger import get_logger, stop_log_listener
from db.engine import test_db_connection

logger = get_logger("service_lifecycle")

AIClientInitializer = Callable[..., Awaitable[None]]
AIClientResetter = Callable[[], Awaitable[None]]
AdapterRegistryInitializer = Callable[[], None]


@dataclass(frozen=True, slots=True)
class RuntimeServices:
    app_context: AppContext
    http_client: httpx.AsyncClient


async def check_database_ready(context: AppContext | None = None) -> bool:
    context = context or init_default_app_context()
    set_default_app_context(context)
    await context.ensure_db()
    return await test_db_connection()


async def start_runtime_services(
    config: AppConfig,
    *,
    broker: Any | None = None,
    start_broker: bool = False,
    initialize_logger: Callable[[], object] | None = None,
    initialize_observability: Callable[[], None] | None = None,
    initialize_redis_client: bool = False,
    initialize_adapter_registry: bool = True,
    initialize_adapter_registry_hook: AdapterRegistryInitializer | None = None,
    initialize_ai_client: bool = False,
    initialize_ai_client_hook: AIClientInitializer | None = None,
    context: AppContext | None = None,
) -> RuntimeServices:
    context = context or get_or_create_default_app_context(config)
    set_default_app_context(context)

    if initialize_logger is not None:
        initialize_logger()
    if initialize_observability is not None:
        initialize_observability()

    if initialize_adapter_registry:
        if initialize_adapter_registry_hook is None:
            logger.warning(
                "[Lifecycle] adapter registry initialization requested but no initializer hook is registered"
            )
        else:
            initialize_adapter_registry_hook()

    http_client = await context.ensure_http_client()
    await context.ensure_db()
    if initialize_redis_client:
        context.ensure_redis_client()

    if initialize_ai_client and config.ai.ENABLE_AI_ANALYSIS and config.ai.OPENAI_API_KEY:
        if initialize_ai_client_hook is None:
            logger.warning("[Lifecycle] AI client initialization requested but no initializer hook is registered")
        else:
            await initialize_ai_client_hook(http_client=http_client)

    if start_broker and broker is not None:
        await broker.startup()

    return RuntimeServices(app_context=context, http_client=http_client)


async def stop_runtime_services(
    config: AppConfig,
    *,
    broker: Any | None = None,
    stop_broker: bool = False,
    reset_ai_client: bool = False,
    reset_ai_client_hook: AIClientResetter | None = None,
    dispose_redis_client: bool = True,
    shutdown_observability: Callable[[], None] | None = None,
    stop_logger: bool = False,
    context: AppContext | None = None,
) -> None:
    context = context or get_or_create_default_app_context(config)

    if stop_broker and broker is not None:
        await broker.shutdown()

    if reset_ai_client:
        if reset_ai_client_hook is None:
            logger.warning("[Lifecycle] AI client reset requested but no reset hook is registered")
        else:
            await reset_ai_client_hook()
    await context.close(close_redis=dispose_redis_client)
    if context is get_default_app_context():
        set_default_app_context(None)

    if shutdown_observability is not None:
        shutdown_observability()
    if stop_logger:
        stop_log_listener()

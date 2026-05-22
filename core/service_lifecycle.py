"""Shared startup and shutdown helpers for API and worker processes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from adapters.ecosystem_adapters import initialize_adapters
from core.app_context import (
    AppContext,
    get_default_app_context,
    get_or_create_default_app_context,
    set_default_app_context,
)
from core.config import UnifiedConfigManager
from core.logger import stop_log_listener
from db.engine import test_db_connection
from services.analysis.ai_analyzer import initialize_openai_client, reset_openai_client


@dataclass(frozen=True, slots=True)
class RuntimeServices:
    app_context: AppContext
    http_client: httpx.AsyncClient


async def check_database_ready(context: AppContext | None = None) -> bool:
    context = context or get_or_create_default_app_context()
    set_default_app_context(context)
    await context.ensure_db()
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
    context: AppContext | None = None,
) -> RuntimeServices:
    context = context or get_or_create_default_app_context(config)
    set_default_app_context(context)

    if initialize_logger is not None:
        initialize_logger()
    if initialize_observability is not None:
        initialize_observability()

    if initialize_adapter_registry:
        initialize_adapters()

    http_client = await context.ensure_http_client()
    await context.ensure_db()
    if initialize_redis_client:
        context.ensure_redis_client()

    if initialize_ai_client and config.ai.ENABLE_AI_ANALYSIS and config.ai.OPENAI_API_KEY:
        await initialize_openai_client(http_client=http_client)

    if start_broker and broker is not None:
        await broker.startup()

    return RuntimeServices(app_context=context, http_client=http_client)


async def stop_runtime_services(
    config: UnifiedConfigManager,
    *,
    broker: Any | None = None,
    stop_broker: bool = False,
    reset_ai_client: bool = False,
    dispose_redis_client: bool = True,
    shutdown_observability: Callable[[], None] | None = None,
    stop_logger: bool = False,
    context: AppContext | None = None,
) -> None:
    context = context or get_or_create_default_app_context(config)

    if stop_broker and broker is not None:
        await broker.shutdown()

    if reset_ai_client:
        await reset_openai_client()
    await context.close(close_redis=dispose_redis_client)
    if context is get_default_app_context():
        set_default_app_context(None)

    if shutdown_observability is not None:
        shutdown_observability()
    if stop_logger:
        stop_log_listener()

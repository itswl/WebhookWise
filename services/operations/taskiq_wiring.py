"""TaskIQ entrypoint wiring.

This module imports task definitions and registers worker/scheduler lifecycle
hooks without making ``core.taskiq_broker`` depend on ``services``.
"""

from __future__ import annotations

import asyncio
import logging
import secrets

from taskiq import TaskiqEvents

import services.operations.tasks as _tasks  # noqa: F401
from adapters.ecosystem_adapters import initialize_adapters
from core.config.defaults import get_settings
from core.taskiq_broker import broker, dynamic_schedule_source, load_taskiq_broker_settings, scheduler
from services.analysis.ai_llm_client import initialize_openai_client, reset_openai_client

__all__ = ("broker", "dynamic_schedule_source", "scheduler")

logger = logging.getLogger("webhook_service.taskiq")
_settings = load_taskiq_broker_settings()
_jitter_rng = secrets.SystemRandom()


@broker.on_event(TaskiqEvents.CLIENT_STARTUP)
async def scheduler_startup_event(state: object) -> None:
    """Scheduler process startup hook."""
    if _settings.run_mode != "scheduler":
        logger.debug("[TaskIQ] Skipping scheduler runtime initialization run_mode=%s", _settings.run_mode)
        return

    from core.observability import setup_observability
    from core.web.startup_checks import validate_startup_security

    validate_startup_security(get_settings())
    setup_observability()


@broker.on_event(TaskiqEvents.CLIENT_SHUTDOWN)
async def scheduler_shutdown_event(state: object) -> None:
    """Scheduler process shutdown hook."""
    if _settings.run_mode != "scheduler":
        logger.debug("[TaskIQ] Skipping scheduler runtime shutdown run_mode=%s", _settings.run_mode)
        return

    from core.observability import shutdown_observability

    shutdown_observability()


@broker.on_event(TaskiqEvents.WORKER_STARTUP)
async def worker_startup_event(state: object) -> None:
    """Lifecycle event fired when the worker process starts."""
    if _settings.run_mode != "worker":
        logger.debug("[TaskIQ] Skipping worker runtime initialization run_mode=%s", _settings.run_mode)
        return

    from core.app_context import init_default_app_context
    from core.logger import setup_logger
    from core.observability import setup_observability
    from core.service_lifecycle import start_runtime_services
    from core.web.startup_checks import validate_startup_security

    if _settings.worker_startup_jitter_seconds > 0:
        await asyncio.sleep(_jitter_rng.uniform(0.0, _settings.worker_startup_jitter_seconds))

    context = init_default_app_context(get_settings())
    validate_startup_security(context.config)
    await start_runtime_services(
        context.config,
        context=context,
        initialize_logger=setup_logger,
        initialize_observability=setup_observability,
        initialize_redis_client=True,
        initialize_adapter_registry=True,
        initialize_adapter_registry_hook=initialize_adapters,
        initialize_ai_client=True,
        initialize_ai_client_hook=initialize_openai_client,
    )

    # Catch-up: send any enabled periodic report whose most recent scheduled fire
    # was missed while no scheduler was alive (deploy/restart landing on the cron
    # minute). Idempotent via a Redis last-sent marker. Best-effort — a failure
    # here must not block worker startup.
    try:
        from services.operations.periodic_report import run_report_catchup

        outcomes = await run_report_catchup()
        if any(v == "sent" for v in outcomes.values()):
            logger.info("[TaskIQ] periodic-report catch-up outcomes=%s", outcomes)
    except Exception:  # noqa: BLE001 - never let catch-up break worker startup
        logger.warning("[TaskIQ] periodic-report catch-up failed", exc_info=True)


@broker.on_event(TaskiqEvents.WORKER_SHUTDOWN)
async def worker_shutdown_event(state: object) -> None:
    """Lifecycle event fired when the worker process shuts down."""
    if _settings.run_mode != "worker":
        logger.debug("[TaskIQ] Skipping worker runtime shutdown run_mode=%s", _settings.run_mode)
        return

    from core.app_context import get_default_app_context, init_default_app_context
    from core.observability import shutdown_observability
    from core.service_lifecycle import stop_runtime_services
    from services.analysis.ai_usage import flush_ai_usage

    context = get_default_app_context() or init_default_app_context(get_settings())
    # Buffered AI-usage rows must land before the DB engine goes away.
    await flush_ai_usage()
    await stop_runtime_services(
        context.config,
        context=context,
        reset_ai_client=True,
        reset_ai_client_hook=reset_openai_client,
    )
    shutdown_observability()

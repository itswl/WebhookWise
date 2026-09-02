"""TaskIQ entrypoint wiring.

This module imports task definitions and registers worker/scheduler lifecycle
hooks without making ``core.taskiq_broker`` depend on ``services``.
"""

from __future__ import annotations

import asyncio
import logging
import secrets

from taskiq import TaskiqEvents

import services.operations.scheduled_reports as _scheduled_reports  # noqa: F401
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

    from core.app_context import init_default_app_context
    from core.observability import setup_observability
    from core.runtime_heartbeat import start_runtime_heartbeat
    from core.web.startup_checks import validate_startup_security

    context = init_default_app_context(get_settings())
    validate_startup_security(context.config)
    setup_observability()
    context.ensure_redis_client()
    await start_runtime_heartbeat("scheduler")


@broker.on_event(TaskiqEvents.CLIENT_SHUTDOWN)
async def scheduler_shutdown_event(state: object) -> None:
    """Scheduler process shutdown hook."""
    if _settings.run_mode != "scheduler":
        logger.debug("[TaskIQ] Skipping scheduler runtime shutdown run_mode=%s", _settings.run_mode)
        return

    from core.app_context import get_default_app_context
    from core.observability import shutdown_observability
    from core.runtime_heartbeat import stop_runtime_heartbeat

    await stop_runtime_heartbeat("scheduler")
    context = get_default_app_context()
    if context is not None:
        await context.close(close_db=False, close_http=False)
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

    from core.runtime_heartbeat import start_runtime_heartbeat
    from services.forwarding.rules import start_rules_invalidation_listener
    from services.operations.runtime_settings import start_runtime_settings_plane
    from services.silences.store import start_silences_invalidation_listener

    await start_rules_invalidation_listener()
    await start_silences_invalidation_listener()
    await start_runtime_settings_plane()
    await start_runtime_heartbeat("worker")

    # A restart registers a new consumer name in the stream group and leaves the
    # old one behind forever (production: 127 consumers, 1 alive). Reap the
    # corpses here, where Redis is up and the group certainly exists.
    # Best-effort — a cleanup must never be why a worker fails to start.
    try:
        from core.redis_streams import reap_idle_stream_consumers

        reaped = await reap_idle_stream_consumers(
            _settings.queue_name,
            _settings.consumer_group_name,
            keep=_settings.consumer_name,
        )
        if reaped:
            logger.info(
                "[TaskIQ] reaped %d idle consumer(s) from stream group %s",
                reaped,
                _settings.consumer_group_name,
            )
    except Exception:  # noqa: BLE001 - never let consumer hygiene break worker startup
        logger.warning("[TaskIQ] stream consumer reap failed", exc_info=True)

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
    from core.runtime_heartbeat import stop_runtime_heartbeat
    from core.service_lifecycle import stop_runtime_services
    from services.analysis.ai_usage import flush_ai_usage
    from services.forwarding.rules import stop_rules_invalidation_listener
    from services.silences.store import stop_silences_invalidation_listener

    context = get_default_app_context() or init_default_app_context(get_settings())
    # Buffered AI-usage rows must land before the DB engine goes away.
    await flush_ai_usage()
    from services.operations.runtime_settings import stop_runtime_settings_plane

    await stop_runtime_heartbeat("worker")
    await stop_runtime_settings_plane()
    await stop_rules_invalidation_listener()
    await stop_silences_invalidation_listener()
    await stop_runtime_services(
        context.config,
        context=context,
        reset_ai_client=True,
        reset_ai_client_hook=reset_openai_client,
    )
    shutdown_observability()

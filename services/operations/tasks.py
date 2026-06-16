"""TaskIQ async task definitions.

Includes:
- webhook_process_task: consumes the webhook queue
- scheduled polling tasks: enqueued by the TaskIQ Scheduler, executed by the Worker
"""

import contextlib
import inspect
import time
import uuid
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from contextvars import Token
from dataclasses import dataclass, field

from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError

from core import redis_client, redis_health
from core.log_context import clear_log_context, set_log_context
from core.logger import get_logger
from core.observability.attributes import WEBHOOK_EVENT_ID, WEBHOOK_OUTCOME, WEBHOOK_SOURCE
from core.observability.events import emit_event, record_signal
from core.observability.metrics import (
    SCHEDULED_TASK_DURATION_SECONDS,
    SCHEDULED_TASK_LAG_SECONDS,
    SCHEDULED_TASK_LAST_SUCCESS_UNIXTIME,
    SCHEDULED_TASK_RUNS_TOTAL,
    WEBHOOK_RUNNING_TASKS,
    WORKER_TASK_DURATION_SECONDS,
    WORKER_TASKS_TOTAL,
)
from core.observability.tracing import (
    extract_trace_id_from_headers,
    generate_trace_id,
    inject_trace_headers,
    otel_span,
    reset_fallback_trace_id,
    set_fallback_trace_id,
    trace_context_from_headers,
)
from core.redis_health import scheduled_task_lock
from core.redis_lua import ALERT_RELEASE_LOCK_IF_OWNER
from core.taskiq_broker import broker
from services.operations.policies import TaskRuntimePolicy

logger = get_logger("tasks")

_RELEASE_IF_OWNER_LUA = ALERT_RELEASE_LOCK_IF_OWNER
_SCHEDULING_ERRORS = (OSError, RedisError, RuntimeError, TimeoutError)
_TASK_PROCESSING_ERRORS = (OSError, RuntimeError, SQLAlchemyError, ValueError)


@dataclass(slots=True)
class _WebhookTaskContext:
    source: str
    raw_headers: dict[str, str]
    raw_body: str
    client_ip: str
    request_id: str | None
    received_at: str | None
    ingest_retry_count: int
    traceparent: str | None
    trace_headers: dict[str, str]


@dataclass(slots=True)
class _ScheduledTaskRuntime:
    last_success_by_name: dict[str, float] = field(default_factory=dict)

    def record_success(self, name: str, interval_seconds: int, now: float) -> float:
        prev = self.last_success_by_name.get(name)
        self.last_success_by_name[name] = now
        return max(0.0, now - prev - float(interval_seconds)) if prev is not None else 0.0

    def lag_since_success(self, name: str, interval_seconds: int, now: float) -> float | None:
        last = self.last_success_by_name.get(name)
        if last is None:
            return None
        return max(0.0, now - last - float(interval_seconds))


_scheduled_task_runtime = _ScheduledTaskRuntime()


def _background_scan_interval_seconds() -> int:
    return TaskRuntimePolicy.from_config().background_scan_interval_seconds


def _metrics_refresh_interval_seconds() -> int:
    return TaskRuntimePolicy.from_config().metrics_refresh_interval_seconds


def _maintenance_cron() -> str:
    return f"0 {TaskRuntimePolicy.from_config().maintenance_hour} * * *"


async def _handle_raw_webhook_failure(
    *,
    source: str,
    raw_headers: dict[str, str],
    raw_body: str,
    client_ip: str,
    request_id: str | None,
    received_at: str | None,
    ingest_retry_count: int,
    err: Exception,
    traceparent: str | None = None,
) -> None:
    from core.observability.metrics import WEBHOOK_DEAD_LETTER_TOTAL, WEBHOOK_PROCESSING_STATUS_TOTAL
    from core.retry_policies import retry_policy
    from services.operations.taskiq_retry_scheduler import compute_backoff_delay, schedule_webhook_ingest_retry
    from services.webhooks.ingest_failure import record_raw_ingest_dead_letter
    from services.webhooks.policies import WebhookRetryPolicy

    policy = WebhookRetryPolicy.from_config()
    retryable = retry_policy.should_retry(err)
    next_retry_count = max(0, int(ingest_retry_count)) + 1

    if retryable and ingest_retry_count < policy.max_retries:
        delay = compute_backoff_delay(
            next_retry_count,
            initial_delay=policy.initial_delay,
            max_delay=policy.max_delay,
            multiplier=policy.backoff_multiplier,
        )
        try:
            await schedule_webhook_ingest_retry(
                delay_seconds=delay,
                source=source,
                raw_headers=raw_headers,
                raw_body=raw_body,
                client_ip=client_ip,
                request_id=request_id,
                received_at=received_at,
                ingest_retry_count=next_retry_count,
                traceparent=traceparent,
            )
            WEBHOOK_PROCESSING_STATUS_TOTAL.labels(status="retry").inc()
            logger.error(
                "[Tasks] raw webhook processing failed, retry scheduled request_id=%s source=%s retry=%s/%s delay=%ss error=%s",
                request_id,
                source,
                next_retry_count,
                policy.max_retries,
                delay,
                err,
                exc_info=True,
            )
            return
        except _SCHEDULING_ERRORS as schedule_err:
            err = RuntimeError(f"raw ingest retry schedule failed: {schedule_err}; process_error={err}")
            logger.critical(
                "[Tasks] raw webhook retry scheduling failed, will write to dead-letter request_id=%s source=%s error=%s",
                request_id,
                source,
                schedule_err,
                exc_info=True,
            )

    event_id = await record_raw_ingest_dead_letter(
        source=source,
        raw_headers=raw_headers,
        raw_body=raw_body,
        client_ip=client_ip,
        request_id=request_id,
        received_at=received_at,
        retry_count=ingest_retry_count,
        retryable=retryable,
        err=err,
    )
    WEBHOOK_DEAD_LETTER_TOTAL.inc()
    WEBHOOK_PROCESSING_STATUS_TOTAL.labels(status="dead_letter").inc()
    logger.error(
        "[Tasks] raw webhook processing failed, moved to dead-letter event_id=%s request_id=%s source=%s retryable=%s error=%s",
        event_id,
        request_id,
        source,
        retryable,
        err,
        exc_info=True,
    )


@asynccontextmanager
async def _scheduled_task_leader(
    name: str, interval_seconds: int, *, policy: TaskRuntimePolicy | None = None
) -> AsyncIterator[bool]:
    """Best-effort singleton guard for scheduled tasks when scheduler is accidentally scaled."""
    key = scheduled_task_lock(name)
    token = f"{(policy or TaskRuntimePolicy.from_config()).worker_id}:{uuid.uuid4().hex}"
    ttl = max(30, int(interval_seconds) * 2)
    if not await redis_health.ensure_redis_available(f"scheduled_task:{name}:leader"):
        logger.warning("[ScheduledTask] Redis single-instance lock unavailable, skipping scheduling name=%s", name)
        yield False
        return

    try:
        acquired = await redis_client.redis_set_nx_ex(key, token, ttl)
    except _SCHEDULING_ERRORS as e:
        redis_health.mark_redis_failure(f"scheduled_task:{name}:leader", e)
        logger.warning("[ScheduledTask] Single-instance lock error, skipping scheduling name=%s error=%s", name, e)
        yield False
        return

    try:
        yield acquired
    finally:
        if acquired:
            with contextlib.suppress(*_SCHEDULING_ERRORS):
                await redis_client.redis_eval_int(_RELEASE_IF_OWNER_LUA, 1, key, token)


async def _run_scheduled(name: str, interval_seconds: int, fn: Awaitable[object]) -> None:
    async with _scheduled_task_leader(name, interval_seconds) as is_leader:
        if not is_leader:
            logger.debug("[ScheduledTask] Skipping duplicate execution name=%s interval=%ss", name, interval_seconds)
            if inspect.iscoroutine(fn):
                fn.close()
            return
        await _run_scheduled_locked(name, interval_seconds, fn)


async def _run_scheduled_locked(
    name: str,
    interval_seconds: int,
    fn: Awaitable[object],
    *,
    runtime: _ScheduledTaskRuntime | None = None,
) -> None:
    runtime = runtime or _scheduled_task_runtime
    start = time.time()
    status = "success"
    lag = 0.0
    logger.debug("[ScheduledTask] Periodic task started name=%s interval=%ss", name, interval_seconds)
    with otel_span("scheduler.run", {"scheduler.task.name": name}) as scheduler_span:
        try:
            await fn
            now = time.time()
            lag = runtime.record_success(name, interval_seconds, now)
            SCHEDULED_TASK_LAG_SECONDS.labels(name=name).set(lag)
            SCHEDULED_TASK_LAST_SUCCESS_UNIXTIME.labels(name=name).set(now)
        except BaseException:
            status = "error"
            error_lag = runtime.lag_since_success(name, interval_seconds, time.time())
            if error_lag is not None:
                lag = error_lag
                SCHEDULED_TASK_LAG_SECONDS.labels(name=name).set(lag)
            logger.exception(
                "[ScheduledTask] Periodic task failed name=%s interval=%ss lag=%.3fs",
                name,
                interval_seconds,
                lag,
            )
            raise
        finally:
            duration = time.time() - start
            if scheduler_span is not None:
                scheduler_span.set_attribute("scheduler.task.status", status)
            SCHEDULED_TASK_RUNS_TOTAL.labels(name=name, status=status).inc()
            SCHEDULED_TASK_DURATION_SECONDS.labels(name=name).observe(duration)
            if status == "success":
                logger.debug(
                    "[ScheduledTask] Periodic task succeeded name=%s interval=%ss duration=%.3fs lag=%.3fs",
                    name,
                    interval_seconds,
                    duration,
                    lag,
                )


def _build_webhook_task_context(
    *,
    client_ip: str | None,
    source_name: str,
    raw_headers: dict[str, str] | None,
    raw_body: str | None,
    request_id: str | None,
    received_at: str | None,
    ingest_retry_count: int,
    traceparent: str | None,
) -> _WebhookTaskContext:
    trace_headers = {"traceparent": traceparent or ""} if traceparent else {}
    inject_trace_headers(
        trace_headers,
        request_id=request_id,
        fallback_trace_id=extract_trace_id_from_headers(trace_headers) or generate_trace_id(),
    )
    return _WebhookTaskContext(
        source=source_name or "unknown",
        raw_headers=raw_headers or {},
        raw_body=raw_body or "",
        client_ip=client_ip or "",
        request_id=request_id,
        received_at=received_at,
        ingest_retry_count=ingest_retry_count,
        traceparent=trace_headers.get("traceparent") or traceparent,
        trace_headers=trace_headers,
    )


def _start_webhook_task(ctx: _WebhookTaskContext) -> None:
    clear_log_context()
    set_log_context(request_id=ctx.request_id, webhook_source=ctx.source)
    logger.debug(
        "[Tasks] Webhook task started request_id=%s source=%s retry=%s",
        ctx.request_id,
        ctx.source,
        ctx.ingest_retry_count,
    )
    emit_event(
        "webhook.task.started",
        {
            WEBHOOK_EVENT_ID: 0,
            WEBHOOK_SOURCE: ctx.source,
            "webhook.raw_ingest": True,
            "retry.count": ctx.ingest_retry_count,
        },
    )


def _set_webhook_task_fallback_trace(ctx: _WebhookTaskContext) -> Token[str] | None:
    fallback_trace_id = extract_trace_id_from_headers(ctx.trace_headers)
    return set_fallback_trace_id(fallback_trace_id) if fallback_trace_id else None


def _reset_webhook_task_fallback_trace(token: Token[str] | None) -> None:
    if token is None:
        return
    try:
        reset_fallback_trace_id(token)
    except ValueError:
        logger.debug("[Tasks] fallback trace context already reset", exc_info=True)


def _finish_webhook_task(ctx: _WebhookTaskContext, outcome: str, task_start: float) -> None:
    duration_ms = int((time.perf_counter() - task_start) * 1000)
    logger.info(
        "[Tasks] Webhook task finished request_id=%s source=%s outcome=%s duration=%dms",
        ctx.request_id,
        ctx.source,
        outcome,
        duration_ms,
    )
    attributes = {
        WEBHOOK_EVENT_ID: 0,
        WEBHOOK_SOURCE: ctx.source,
        WEBHOOK_OUTCOME: outcome,
        "worker.task.duration_ms": duration_ms,
    }
    emit_event("webhook.task.finished", attributes)
    record_signal("webhook.task", outcome, attributes)
    WORKER_TASKS_TOTAL.labels("webhook_process_task", outcome).inc()
    WORKER_TASK_DURATION_SECONDS.labels("webhook_process_task", outcome).observe(time.perf_counter() - task_start)


@broker.task(task_name="webhook_process_task")
async def process_webhook_task(
    client_ip: str | None = None,
    source_name: str = "unknown",
    raw_headers: dict[str, str] | None = None,
    raw_body: str | None = None,
    request_id: str | None = None,
    received_at: str | None = None,
    ingest_retry_count: int = 0,
    traceparent: str | None = None,
) -> None:
    """Process a raw ingested webhook."""
    from services.webhooks.pipeline import handle_webhook_ingest

    ctx = _build_webhook_task_context(
        client_ip=client_ip,
        source_name=source_name,
        raw_headers=raw_headers,
        raw_body=raw_body,
        request_id=request_id,
        received_at=received_at,
        ingest_retry_count=ingest_retry_count,
        traceparent=traceparent,
    )
    task_start = time.perf_counter()
    outcome = "completed"
    _start_webhook_task(ctx)
    fallback_token = _set_webhook_task_fallback_trace(ctx)
    try:
        with (
            trace_context_from_headers(ctx.trace_headers),
            otel_span(
                "worker.webhook_process_task",
                {
                    WEBHOOK_EVENT_ID: 0,
                    WEBHOOK_SOURCE: ctx.source,
                    "retry.count": ctx.ingest_retry_count,
                    "webhook.raw_ingest": True,
                    "worker.task.name": "webhook_process_task",
                },
            ) as worker_span,
        ):
            WEBHOOK_RUNNING_TASKS.inc()
            try:
                await handle_webhook_ingest(
                    source=ctx.source,
                    raw_headers=ctx.raw_headers,
                    raw_body=ctx.raw_body,
                    client_ip=ctx.client_ip,
                    request_id=ctx.request_id,
                    received_at=ctx.received_at,
                )
            except _TASK_PROCESSING_ERRORS as e:
                await _handle_raw_webhook_failure(
                    source=ctx.source,
                    raw_headers=ctx.raw_headers,
                    raw_body=ctx.raw_body,
                    client_ip=ctx.client_ip,
                    request_id=ctx.request_id,
                    received_at=ctx.received_at,
                    ingest_retry_count=ctx.ingest_retry_count,
                    traceparent=ctx.trace_headers["traceparent"] or ctx.traceparent,
                    err=e,
                )
                outcome = "raw_failure_handled"
            finally:
                WEBHOOK_RUNNING_TASKS.dec()
                if worker_span is not None:
                    worker_span.set_attribute("worker.task.status", outcome)
                    worker_span.set_attribute(WEBHOOK_OUTCOME, outcome)
    except BaseException:
        outcome = "error"
        logger.exception(
            "[Tasks] Webhook task exited with an exception request_id=%s source=%s",
            ctx.request_id,
            ctx.source,
        )
        raise
    finally:
        _reset_webhook_task_fallback_trace(fallback_token)
        _finish_webhook_task(ctx, outcome, task_start)


async def run_forward_outbox_task(outbox_id: int) -> None:
    """Execute one transactional forwarding outbox intent."""
    from services.forwarding.outbox import process_forward_outbox_by_id

    start = time.perf_counter()
    status = "success"
    try:
        await process_forward_outbox_by_id(outbox_id)
    except BaseException:
        status = "error"
        raise
    finally:
        WORKER_TASKS_TOTAL.labels("forward_outbox_task", status).inc()
        WORKER_TASK_DURATION_SECONDS.labels("forward_outbox_task", status).observe(time.perf_counter() - start)


@broker.task(task_name="forward_outbox_task")
async def process_forward_outbox_task(outbox_id: int) -> None:
    await run_forward_outbox_task(outbox_id)


@broker.task(task_name="openclaw_poll_task")
async def poll_openclaw_analysis_task(analysis_id: int) -> None:
    """Poll one pending OpenClaw deep-analysis record."""
    from services.analysis.openclaw_poll import poll_deep_analysis_once

    await poll_deep_analysis_once(analysis_id)


@broker.task(
    task_name="scheduled_openclaw_poll_scan",
    schedule=[{"interval": _background_scan_interval_seconds(), "schedule_id": "openclaw_poll_scan_interval"}],
)
async def scheduled_openclaw_poll_scan() -> None:
    from services.analysis.openclaw_poll import run_openclaw_poll_scan

    await _run_scheduled("openclaw_poll_scan", _background_scan_interval_seconds(), run_openclaw_poll_scan())


@broker.task(
    task_name="scheduled_metrics_refresh",
    schedule=[
        {
            "interval": _metrics_refresh_interval_seconds(),
            "schedule_id": "metrics_refresh_interval_seconds",
        }
    ],
)
async def scheduled_metrics_refresh() -> None:
    from services.operations.metrics_poller import refresh_all_metrics

    await _run_scheduled("metrics_refresh", _metrics_refresh_interval_seconds(), refresh_all_metrics())


@broker.task(
    task_name="scheduled_forward_outbox_scan",
    schedule=[{"interval": _background_scan_interval_seconds(), "schedule_id": "forward_outbox_scan_interval"}],
)
async def scheduled_forward_outbox_scan() -> None:
    from services.forwarding.outbox_scanner import run_forward_outbox_scan

    await _run_scheduled("forward_outbox_scan", _background_scan_interval_seconds(), run_forward_outbox_scan())


@broker.task(task_name="scheduled_data_maintenance", schedule=[{"cron": _maintenance_cron()}])
async def scheduled_data_maintenance() -> None:
    from services.operations.data_maintenance import cleanup_old_data_by_policy

    await _run_scheduled("data_maintenance", 86400, cleanup_old_data_by_policy())


def _daily_report_cron() -> str:
    from core.app_context import get_config_manager

    return str(get_config_manager().notifications.DAILY_REPORT_CRON)


def _weekly_report_cron() -> str:
    from core.app_context import get_config_manager

    return str(get_config_manager().notifications.WEEKLY_REPORT_CRON)


def _monthly_report_cron() -> str:
    from core.app_context import get_config_manager

    return str(get_config_manager().notifications.MONTHLY_REPORT_CRON)


@broker.task(task_name="scheduled_daily_report", schedule=[{"cron": _daily_report_cron()}])
async def scheduled_daily_report() -> None:
    from services.operations.periodic_report import generate_and_send_daily_report

    # Internally a no-op unless DAILY_REPORT_ENABLED; the leader lock prevents
    # duplicate sends if more than one scheduler is ever running.
    await _run_scheduled("daily_report", 86400, generate_and_send_daily_report())


@broker.task(task_name="scheduled_weekly_report", schedule=[{"cron": _weekly_report_cron()}])
async def scheduled_weekly_report() -> None:
    from services.operations.periodic_report import generate_and_send_weekly_report

    # Internally a no-op unless WEEKLY_REPORT_ENABLED; the leader lock prevents
    # duplicate sends if more than one scheduler is ever running.
    await _run_scheduled("weekly_report", 86400, generate_and_send_weekly_report())


@broker.task(task_name="scheduled_monthly_report", schedule=[{"cron": _monthly_report_cron()}])
async def scheduled_monthly_report() -> None:
    from services.operations.periodic_report import generate_and_send_monthly_report

    # Internally a no-op unless MONTHLY_REPORT_ENABLED; the leader lock prevents
    # duplicate sends if more than one scheduler is ever running.
    await _run_scheduled("monthly_report", 86400, generate_and_send_monthly_report())

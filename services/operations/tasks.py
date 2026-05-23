"""TaskIQ 异步任务定义。

包括：
- webhook_process_task：消费 webhook 队列
- 定时轮询任务：由 TaskIQ Scheduler 触发入队，由 Worker 执行
"""

import asyncio
import contextlib
import inspect
import time
import uuid
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from contextvars import Token
from dataclasses import dataclass

from core import redis_client, redis_health
from core.log_context import clear_log_context, set_log_context
from core.logger import get_logger
from core.observability.attributes import WEBHOOK_OUTCOME
from core.observability.events import emit_event
from core.observability.metrics import (
    SCHEDULED_TASK_DURATION_SECONDS,
    SCHEDULED_TASK_LAG_SECONDS,
    SCHEDULED_TASK_LAST_SUCCESS_UNIXTIME,
    SCHEDULED_TASK_RUNS_TOTAL,
    WEBHOOK_RUNNING_TASKS,
    WORKER_TASK_DURATION_SECONDS,
    WORKER_TASKS_TOTAL,
)
from core.observability.signals import record_signal
from core.observability.tracing import (
    build_traceparent,
    extract_trace_id_from_headers,
    reset_fallback_trace_id,
    set_fallback_trace_id,
    set_span_error,
    trace_context_from_headers,
)
from core.observability.tracing import span as otel_span
from core.redis_health import scheduled_task_lock, webhook_global_task_slots
from core.redis_lua import ALERT_RELEASE_LOCK_IF_OWNER
from core.taskiq_broker import broker
from services.forwarding.outbox import configure_forward_outbox_schedulers
from services.operations.policies import TaskRuntimePolicy, TaskSlotManager

logger = get_logger("tasks")

_last_success_by_name: dict[str, float] = {}
_webhook_task_semaphore: asyncio.Semaphore | None = None
_webhook_task_semaphore_limit = 0

_WEBHOOK_TASK_SLOT_KEY = webhook_global_task_slots()
_RELEASE_IF_OWNER_LUA = ALERT_RELEASE_LOCK_IF_OWNER


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


@asynccontextmanager
async def _local_webhook_task_slot(limit: int) -> AsyncIterator[None]:
    global _webhook_task_semaphore, _webhook_task_semaphore_limit
    if _webhook_task_semaphore is None or _webhook_task_semaphore_limit != limit:
        _webhook_task_semaphore = asyncio.Semaphore(limit)
        _webhook_task_semaphore_limit = limit
    async with _webhook_task_semaphore:
        yield


async def _enqueue_forward_outbox(outbox_id: int) -> None:
    await process_forward_outbox_task.kiq(outbox_id=outbox_id)


async def _schedule_forward_outbox_retry(outbox_id: int, delay_seconds: int) -> None:
    from services.operations.taskiq_retry_scheduler import schedule_forward_outbox

    await schedule_forward_outbox(outbox_id, delay_seconds)


configure_forward_outbox_schedulers(
    enqueue_outbox=_enqueue_forward_outbox,
    schedule_retry=_schedule_forward_outbox_retry,
)


def _task_policy(policy: TaskRuntimePolicy | None = None) -> TaskRuntimePolicy:
    return policy or TaskRuntimePolicy.from_config()


def _webhook_slot_lease_seconds(policy: TaskRuntimePolicy | None = None) -> int:
    return _task_policy(policy).webhook_task_slot_lease_seconds


def _task_slot_manager() -> TaskSlotManager:
    return TaskSlotManager(_WEBHOOK_TASK_SLOT_KEY)


@asynccontextmanager
async def _distributed_webhook_task_slot(limit: int, *, policy: TaskRuntimePolicy | None = None) -> AsyncIterator[None]:
    poll_interval = _task_policy(policy).webhook_task_poll_interval_seconds
    slot_manager = _task_slot_manager()
    member: str | None = None
    try:
        while True:
            if not await redis_health.ensure_redis_available("tasks:webhook_task_slot"):
                logger.warning("[Tasks] Redis 全局并发令牌不可用，暂停处理直到 Redis 恢复")
                await asyncio.sleep(poll_interval)
                continue
            try:
                member = await slot_manager.acquire()
                if member:
                    break
            except Exception as e:
                redis_health.mark_redis_failure("tasks:webhook_task_slot", e)
                logger.warning("[Tasks] Redis 全局并发令牌异常，暂停处理直到 Redis 恢复: %s", e)
            await asyncio.sleep(poll_interval)

        yield
    finally:
        if member:
            with contextlib.suppress(Exception):
                await slot_manager.release(member)


@asynccontextmanager
async def _webhook_task_slot(*, policy: TaskRuntimePolicy | None = None) -> AsyncIterator[None]:
    runtime_policy = _task_policy(policy)
    limit = runtime_policy.max_concurrent_webhook_tasks
    if limit <= 0:
        yield
        return
    async with _distributed_webhook_task_slot(limit, policy=runtime_policy):
        yield


def _background_scan_interval_seconds(policy: TaskRuntimePolicy | None = None) -> int:
    return _task_policy(policy).background_scan_interval_seconds


def _metrics_refresh_interval_seconds(policy: TaskRuntimePolicy | None = None) -> int:
    return _task_policy(policy).metrics_refresh_interval_seconds


def _maintenance_cron(policy: TaskRuntimePolicy | None = None) -> str:
    return f"0 {_task_policy(policy).maintenance_hour} * * *"


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
            if traceparent:
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
            else:
                await schedule_webhook_ingest_retry(
                    delay_seconds=delay,
                    source=source,
                    raw_headers=raw_headers,
                    raw_body=raw_body,
                    client_ip=client_ip,
                    request_id=request_id,
                    received_at=received_at,
                    ingest_retry_count=next_retry_count,
                )
            WEBHOOK_PROCESSING_STATUS_TOTAL.labels(status="retry").inc()
            logger.error(
                "[Tasks] raw webhook 处理失败，已调度重试 request_id=%s source=%s retry=%s/%s delay=%ss error=%s",
                request_id,
                source,
                next_retry_count,
                policy.max_retries,
                delay,
                err,
                exc_info=True,
            )
            return
        except Exception as schedule_err:
            err = RuntimeError(f"raw ingest retry schedule failed: {schedule_err}; process_error={err}")
            logger.critical(
                "[Tasks] raw webhook 重试调度失败，将写入 dead-letter request_id=%s source=%s error=%s",
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
        "[Tasks] raw webhook 处理失败，已进入 dead-letter event_id=%s request_id=%s source=%s retryable=%s error=%s",
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
    token = f"{_task_policy(policy).worker_id}:{uuid.uuid4().hex}"
    ttl = max(30, int(interval_seconds) * 2)
    if not await redis_health.ensure_redis_available(f"scheduled_task:{name}:leader"):
        logger.warning("[ScheduledTask] Redis 单实例锁不可用，跳过调度 name=%s", name)
        yield False
        return

    try:
        acquired = await redis_client.redis_set_nx_ex(key, token, ttl)
    except Exception as e:
        redis_health.mark_redis_failure(f"scheduled_task:{name}:leader", e)
        logger.warning("[ScheduledTask] 单实例锁异常，跳过调度 name=%s error=%s", name, e)
        yield False
        return

    try:
        yield acquired
    finally:
        if acquired:
            with contextlib.suppress(Exception):
                await redis_client.redis_eval_int(_RELEASE_IF_OWNER_LUA, 1, key, token)


async def _run_scheduled(name: str, interval_seconds: int, fn: Awaitable[object]) -> None:
    async with _scheduled_task_leader(name, interval_seconds) as is_leader:
        if not is_leader:
            logger.debug("[ScheduledTask] 跳过重复调度 name=%s", name)
            if inspect.iscoroutine(fn):
                fn.close()
            return
        await _run_scheduled_locked(name, interval_seconds, fn)


async def _run_scheduled_locked(name: str, interval_seconds: int, fn: Awaitable[object]) -> None:
    start = time.time()
    status = "success"
    with otel_span("scheduler.run", {"scheduler.task.name": name}) as scheduler_span:
        try:
            await fn
            now = time.time()
            prev = _last_success_by_name.get(name)
            if prev is not None:
                lag = max(0.0, now - prev - float(interval_seconds))
                SCHEDULED_TASK_LAG_SECONDS.labels(name=name).set(lag)
            else:
                SCHEDULED_TASK_LAG_SECONDS.labels(name=name).set(0.0)
            _last_success_by_name[name] = now
            SCHEDULED_TASK_LAST_SUCCESS_UNIXTIME.labels(name=name).set(now)
        except Exception:
            status = "error"
            last = _last_success_by_name.get(name)
            if last is not None:
                lag = max(0.0, time.time() - last - float(interval_seconds))
                SCHEDULED_TASK_LAG_SECONDS.labels(name=name).set(lag)
            raise
        finally:
            if scheduler_span is not None:
                scheduler_span.set_attribute("scheduler.task.status", status)
            SCHEDULED_TASK_RUNS_TOTAL.labels(name=name, status=status).inc()
            SCHEDULED_TASK_DURATION_SECONDS.labels(name=name).observe(time.time() - start)


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
    trace_headers = {"traceparent": traceparent or ""}
    if not trace_headers["traceparent"] and request_id:
        trace_headers["traceparent"] = build_traceparent(request_id)
    return _WebhookTaskContext(
        source=source_name or "unknown",
        raw_headers=raw_headers or {},
        raw_body=raw_body or "",
        client_ip=client_ip or "",
        request_id=request_id,
        received_at=received_at,
        ingest_retry_count=ingest_retry_count,
        traceparent=traceparent,
        trace_headers=trace_headers,
    )


def _start_webhook_task(ctx: _WebhookTaskContext) -> None:
    clear_log_context()
    set_log_context(request_id=ctx.request_id, source=ctx.source)
    logger.info(
        "[Tasks] Webhook 任务开始 request_id=%s source=%s retry=%s",
        ctx.request_id,
        ctx.source,
        ctx.ingest_retry_count,
    )
    emit_event(
        "webhook.task.started",
        {
            "event_id": 0,
            "source": ctx.source,
            "webhook.raw_ingest": True,
            "retry.count": ctx.ingest_retry_count,
        },
    )


def _set_webhook_task_fallback_trace(ctx: _WebhookTaskContext) -> Token[str] | None:
    fallback_trace_id = extract_trace_id_from_headers(ctx.trace_headers) or (ctx.request_id or "")
    return set_fallback_trace_id(fallback_trace_id) if fallback_trace_id else None


def _reset_webhook_task_fallback_trace(token: Token[str] | None) -> None:
    if token is None:
        return
    try:
        reset_fallback_trace_id(token)
    except ValueError:
        logger.debug("[Tasks] fallback trace context already reset", exc_info=True)


async def _run_webhook_ingest_with_failure_handling(ctx: _WebhookTaskContext) -> str:
    from services.webhooks.pipeline import handle_webhook_ingest

    async with _webhook_task_slot():
        WEBHOOK_RUNNING_TASKS.inc()
        try:
            try:
                await handle_webhook_ingest(
                    source=ctx.source,
                    raw_headers=ctx.raw_headers,
                    raw_body=ctx.raw_body,
                    client_ip=ctx.client_ip,
                    request_id=ctx.request_id,
                    received_at=ctx.received_at,
                )
                return "completed"
            except Exception as e:
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
                return "raw_failure_handled"
        finally:
            WEBHOOK_RUNNING_TASKS.dec()


async def _run_webhook_task_span(ctx: _WebhookTaskContext) -> str:
    outcome = "completed"
    with (
        trace_context_from_headers(ctx.trace_headers),
        otel_span(
            "worker.webhook_process_task",
            {
                "event_id": 0,
                "source": ctx.source,
                "retry.count": ctx.ingest_retry_count,
                "webhook.raw_ingest": True,
                "worker.task.name": "webhook_process_task",
            },
        ) as worker_span,
    ):
        try:
            outcome = await _run_webhook_ingest_with_failure_handling(ctx)
            return outcome
        except Exception as exc:
            outcome = "error"
            set_span_error(worker_span, exc)
            raise
        finally:
            if worker_span is not None:
                worker_span.set_attribute("worker.task.status", outcome)
                worker_span.set_attribute(WEBHOOK_OUTCOME, outcome)


def _finish_webhook_task(ctx: _WebhookTaskContext, outcome: str, task_start: float) -> None:
    duration_ms = int((time.perf_counter() - task_start) * 1000)
    logger.info(
        "[Tasks] Webhook 任务结束 request_id=%s source=%s outcome=%s duration=%dms",
        ctx.request_id,
        ctx.source,
        outcome,
        duration_ms,
    )
    attributes = {
        "event_id": 0,
        "source": ctx.source,
        WEBHOOK_OUTCOME: outcome,
        "duration.ms": duration_ms,
    }
    emit_event("webhook.task.finished", attributes)
    record_signal("webhook.task", outcome, attributes)
    WORKER_TASKS_TOTAL.labels("webhook_process_task", outcome).inc()
    WORKER_TASK_DURATION_SECONDS.labels("webhook_process_task", outcome).observe(time.perf_counter() - task_start)


async def run_webhook_task(
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
        outcome = await _run_webhook_task_span(ctx)
    except Exception:
        outcome = "error"
        logger.exception(
            "[Tasks] Webhook 任务异常退出 request_id=%s source=%s",
            ctx.request_id,
            ctx.source,
        )
        raise
    finally:
        _reset_webhook_task_fallback_trace(fallback_token)
        _finish_webhook_task(ctx, outcome, task_start)


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
    await run_webhook_task(
        client_ip=client_ip,
        source_name=source_name,
        raw_headers=raw_headers,
        raw_body=raw_body,
        request_id=request_id,
        received_at=received_at,
        ingest_retry_count=ingest_retry_count,
        traceparent=traceparent,
    )


async def run_forward_outbox_task(outbox_id: int) -> None:
    """Execute one transactional forwarding outbox intent."""
    from services.forwarding.outbox import process_forward_outbox_by_id

    start = time.perf_counter()
    status = "success"
    try:
        await process_forward_outbox_by_id(outbox_id)
    except Exception:
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
    from services.analysis.openclaw_poller import poll_deep_analysis_once

    await poll_deep_analysis_once(analysis_id)


@broker.task(
    task_name="scheduled_openclaw_poll_scan",
    schedule=[{"interval": _background_scan_interval_seconds(), "schedule_id": "openclaw_poll_scan_interval"}],
)
async def scheduled_openclaw_poll_scan() -> None:
    from services.analysis.openclaw_poller import run_openclaw_poll_scan

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

    async with _scheduled_task_leader("data_maintenance", 86400) as is_leader:
        if not is_leader:
            logger.debug("[ScheduledTask] 跳过重复调度 name=data_maintenance")
            return
        await _run_data_maintenance_locked(cleanup_old_data_by_policy())


async def _run_data_maintenance_locked(fn: Awaitable[object]) -> None:
    start = time.time()
    status = "success"
    with otel_span("scheduler.run", {"scheduler.task.name": "data_maintenance"}) as scheduler_span:
        try:
            await fn
            SCHEDULED_TASK_LAST_SUCCESS_UNIXTIME.labels(name="data_maintenance").set(time.time())
            SCHEDULED_TASK_LAG_SECONDS.labels(name="data_maintenance").set(0.0)
        except Exception:
            status = "error"
            raise
        finally:
            if scheduler_span is not None:
                scheduler_span.set_attribute("scheduler.task.status", status)
            SCHEDULED_TASK_RUNS_TOTAL.labels(name="data_maintenance", status=status).inc()
            SCHEDULED_TASK_DURATION_SECONDS.labels(name="data_maintenance").observe(time.time() - start)

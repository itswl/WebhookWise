"""TaskIQ 异步任务定义。

包括：
- webhook_process_task：消费 webhook 队列
- 定时轮询任务：由 TaskIQ Scheduler 触发入队，由 Worker 执行
"""

import asyncio
import contextlib
import inspect
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager

from core.config import Config
from core.metrics import (
    SCHEDULED_TASK_DURATION_SECONDS,
    SCHEDULED_TASK_LAG_SECONDS,
    SCHEDULED_TASK_LAST_SUCCESS_UNIXTIME,
    SCHEDULED_TASK_RUNS_TOTAL,
)
from core.taskiq_broker import broker

logger = logging.getLogger("webhook_service.tasks")

_last_success_by_name: dict[str, float] = {}
_webhook_task_semaphore: asyncio.Semaphore | None = None
_webhook_task_semaphore_limit = 0

_RELEASE_IF_OWNER_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
end
return 0
"""


@asynccontextmanager
async def _webhook_task_slot() -> AsyncIterator[None]:
    global _webhook_task_semaphore, _webhook_task_semaphore_limit
    limit = int(Config.server.MAX_CONCURRENT_WEBHOOK_TASKS or 0)
    if limit <= 0:
        yield
        return
    if _webhook_task_semaphore is None or _webhook_task_semaphore_limit != limit:
        _webhook_task_semaphore = asyncio.Semaphore(limit)
        _webhook_task_semaphore_limit = limit
    async with _webhook_task_semaphore:
        yield


@asynccontextmanager
async def _scheduled_task_leader(name: str, interval_seconds: int) -> AsyncIterator[bool]:
    """Best-effort singleton guard for scheduled tasks when scheduler is accidentally scaled."""
    key = f"scheduled-task-lock:{name}"
    token = f"{Config.server.WORKER_ID}:{uuid.uuid4().hex}"
    ttl = max(30, int(interval_seconds) * 2)
    try:
        from core.redis_client import redis_eval_int, redis_set_nx_ex

        acquired = await redis_set_nx_ex(key, token, ttl)
    except Exception as e:
        logger.warning("[ScheduledTask] 单实例锁异常，降级执行 name=%s error=%s", name, e)
        yield True
        return

    try:
        yield acquired
    finally:
        if acquired:
            with contextlib.suppress(Exception):
                await redis_eval_int(_RELEASE_IF_OWNER_LUA, 1, key, token)


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
    try:
        await fn
        SCHEDULED_TASK_RUNS_TOTAL.labels(name=name, status="success").inc()
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
        SCHEDULED_TASK_RUNS_TOTAL.labels(name=name, status="error").inc()
        last = _last_success_by_name.get(name)
        if last is not None:
            lag = max(0.0, time.time() - last - float(interval_seconds))
            SCHEDULED_TASK_LAG_SECONDS.labels(name=name).set(lag)
        raise
    finally:
        SCHEDULED_TASK_DURATION_SECONDS.labels(name=name).observe(time.time() - start)


@broker.task(task_name="webhook_process_task")
async def process_webhook_task(
    event_id: int,
    client_ip: str | None = None,
) -> None:
    """异步处理单条 Webhook 事件"""
    from services.webhooks.pipeline import handle_webhook_process

    logger.info("[Tasks] 异步处理 Webhook 事件: ID=%s", event_id)
    async with _webhook_task_slot():
        await handle_webhook_process(event_id=event_id, client_ip=client_ip or "")


@broker.task(task_name="forward_retry_task")
async def retry_failed_forward_task(failed_forward_id: int) -> None:
    """Retry a single failed-forward audit record."""
    from services.forwarding.retry import retry_failed_forward_by_id

    await retry_failed_forward_by_id(failed_forward_id)


@broker.task(task_name="forward_outbox_task")
async def process_forward_outbox_task(outbox_id: int) -> None:
    """Execute one transactional forwarding outbox intent."""
    from services.forwarding.outbox import process_forward_outbox_by_id

    await process_forward_outbox_by_id(outbox_id)


@broker.task(task_name="openclaw_poll_task")
async def poll_openclaw_analysis_task(analysis_id: int) -> None:
    """Poll one pending OpenClaw deep-analysis record."""
    from services.analysis.openclaw_poller import poll_deep_analysis_once

    await poll_deep_analysis_once(analysis_id)


@broker.task(
    task_name="scheduled_openclaw_poll_scan",
    schedule=[
        {"interval": Config.server.RECOVERY_POLLER_INTERVAL_SECONDS, "schedule_id": "openclaw_poll_scan_interval"}
    ],
)
async def scheduled_openclaw_poll_scan() -> None:
    from services.analysis.openclaw_poller import run_openclaw_poll_scan

    await _run_scheduled("openclaw_poll_scan", Config.server.RECOVERY_POLLER_INTERVAL_SECONDS, run_openclaw_poll_scan())


@broker.task(
    task_name="scheduled_recovery_scan",
    schedule=[
        {"interval": Config.server.RECOVERY_POLLER_INTERVAL_SECONDS, "schedule_id": "recovery_scan_interval_seconds"}
    ],
)
async def scheduled_recovery_scan() -> None:
    from services.operations.recovery_poller import run_recovery_scan

    await _run_scheduled("recovery_scan", Config.server.RECOVERY_POLLER_INTERVAL_SECONDS, run_recovery_scan())


@broker.task(
    task_name="scheduled_metrics_refresh",
    schedule=[
        {
            "interval": Config.server.METRICS_REFRESH_INTERVAL_SECONDS,
            "schedule_id": "metrics_refresh_interval_seconds",
        }
    ],
)
async def scheduled_metrics_refresh() -> None:
    from services.operations.metrics_poller import refresh_all_metrics

    await _run_scheduled("metrics_refresh", Config.server.METRICS_REFRESH_INTERVAL_SECONDS, refresh_all_metrics())


@broker.task(
    task_name="scheduled_forward_outbox_scan",
    schedule=[
        {"interval": Config.server.RECOVERY_POLLER_INTERVAL_SECONDS, "schedule_id": "forward_outbox_scan_interval"}
    ],
)
async def scheduled_forward_outbox_scan() -> None:
    from services.forwarding.outbox import run_forward_outbox_scan

    await _run_scheduled(
        "forward_outbox_scan", Config.server.RECOVERY_POLLER_INTERVAL_SECONDS, run_forward_outbox_scan()
    )


@broker.task(
    task_name="scheduled_data_maintenance", schedule=[{"cron": f"0 {Config.maintenance.MAINTENANCE_HOUR} * * *"}]
)
async def scheduled_data_maintenance() -> None:
    from services.operations.data_maintenance import archive_old_data_by_policy

    async with _scheduled_task_leader("data_maintenance", 86400) as is_leader:
        if not is_leader:
            logger.debug("[ScheduledTask] 跳过重复调度 name=data_maintenance")
            return
        await _run_data_maintenance_locked(archive_old_data_by_policy())


async def _run_data_maintenance_locked(fn: Awaitable[object]) -> None:
    start = time.time()
    try:
        await fn
        SCHEDULED_TASK_RUNS_TOTAL.labels(name="data_maintenance", status="success").inc()
        SCHEDULED_TASK_LAST_SUCCESS_UNIXTIME.labels(name="data_maintenance").set(time.time())
        SCHEDULED_TASK_LAG_SECONDS.labels(name="data_maintenance").set(0.0)
    except Exception:
        SCHEDULED_TASK_RUNS_TOTAL.labels(name="data_maintenance", status="error").inc()
        raise
    finally:
        SCHEDULED_TASK_DURATION_SECONDS.labels(name="data_maintenance").observe(time.time() - start)

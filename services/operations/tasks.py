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

from core.metrics import (
    SCHEDULED_TASK_DURATION_SECONDS,
    SCHEDULED_TASK_LAG_SECONDS,
    SCHEDULED_TASK_LAST_SUCCESS_UNIXTIME,
    SCHEDULED_TASK_RUNS_TOTAL,
    WEBHOOK_RUNNING_TASKS,
)
from core.redis_client import RedisEvalArg
from core.taskiq_broker import broker
from services.operations.policies import TaskRuntimePolicy

logger = logging.getLogger("webhook_service.tasks")

_last_success_by_name: dict[str, float] = {}
_webhook_task_semaphore: asyncio.Semaphore | None = None
_webhook_task_semaphore_limit = 0

_WEBHOOK_TASK_SLOT_KEY = "webhook:global-task-slots"

_ACQUIRE_WEBHOOK_SLOT_LUA = """
redis.call("zremrangebyscore", KEYS[1], "-inf", ARGV[1])
if redis.call("zcard", KEYS[1]) >= tonumber(ARGV[2]) then
    return 0
end
redis.call("zadd", KEYS[1], ARGV[3], ARGV[4])
redis.call("pexpire", KEYS[1], ARGV[5])
return 1
"""

_RENEW_WEBHOOK_SLOT_LUA = """
if redis.call("zscore", KEYS[1], ARGV[1]) then
    redis.call("zadd", KEYS[1], ARGV[2], ARGV[1])
    redis.call("pexpire", KEYS[1], ARGV[3])
    return 1
end
return 0
"""

_RELEASE_WEBHOOK_SLOT_LUA = """
return redis.call("zrem", KEYS[1], ARGV[1])
"""

_RELEASE_IF_OWNER_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
end
return 0
"""


@asynccontextmanager
async def _local_webhook_task_slot(limit: int) -> AsyncIterator[None]:
    global _webhook_task_semaphore, _webhook_task_semaphore_limit
    if _webhook_task_semaphore is None or _webhook_task_semaphore_limit != limit:
        _webhook_task_semaphore = asyncio.Semaphore(limit)
        _webhook_task_semaphore_limit = limit
    async with _webhook_task_semaphore:
        yield


async def _redis_eval_int(script: str, numkeys: int, *args: RedisEvalArg) -> int:
    from core.redis_client import redis_eval_int

    return await redis_eval_int(script, numkeys, *args)


def _task_policy(policy: TaskRuntimePolicy | None = None) -> TaskRuntimePolicy:
    return policy or TaskRuntimePolicy.from_config()


def _webhook_slot_lease_seconds(policy: TaskRuntimePolicy | None = None) -> int:
    return _task_policy(policy).webhook_task_slot_lease_seconds


def _slot_times(lease_seconds: int) -> tuple[int, int, int]:
    now_ms = int(time.time() * 1000)
    lease_ms = int(lease_seconds * 1000)
    # Keep the key slightly longer than one member lease so Redis can clean old slots on the next acquire.
    key_ttl_ms = lease_ms + 30_000
    return now_ms, now_ms + lease_ms, key_ttl_ms


async def _try_acquire_webhook_slot(token: str, limit: int, lease_seconds: int) -> bool:
    now_ms, expires_at_ms, key_ttl_ms = _slot_times(lease_seconds)
    acquired = await _redis_eval_int(
        _ACQUIRE_WEBHOOK_SLOT_LUA,
        1,
        _WEBHOOK_TASK_SLOT_KEY,
        now_ms,
        limit,
        expires_at_ms,
        token,
        key_ttl_ms,
    )
    return bool(acquired)


async def _renew_webhook_slot_until_cancelled(token: str, lease_seconds: int) -> None:
    interval = max(1.0, lease_seconds / 3)
    while True:
        await asyncio.sleep(interval)
        _now_ms, expires_at_ms, key_ttl_ms = _slot_times(lease_seconds)
        renewed = await _redis_eval_int(
            _RENEW_WEBHOOK_SLOT_LUA,
            1,
            _WEBHOOK_TASK_SLOT_KEY,
            token,
            expires_at_ms,
            key_ttl_ms,
        )
        if not renewed:
            logger.warning("[Tasks] Redis 全局并发令牌续期失败，可能已失去 slot token=%s", token)
            return


@asynccontextmanager
async def _distributed_webhook_task_slot(limit: int, *, policy: TaskRuntimePolicy | None = None) -> AsyncIterator[None]:
    runtime_policy = _task_policy(policy)
    token = f"{runtime_policy.worker_id}:{uuid.uuid4().hex}"
    lease_seconds = _webhook_slot_lease_seconds(runtime_policy)
    poll_interval = runtime_policy.webhook_task_poll_interval_seconds
    renew_task: asyncio.Task[None] | None = None
    try:
        while True:
            try:
                if await _try_acquire_webhook_slot(token, limit, lease_seconds):
                    renew_task = asyncio.create_task(_renew_webhook_slot_until_cancelled(token, lease_seconds))
                    break
            except Exception as e:
                logger.warning("[Tasks] Redis 全局并发令牌异常，降级为本进程限流: %s", e)
                async with _local_webhook_task_slot(limit):
                    yield
                return
            await asyncio.sleep(poll_interval)

        yield
    finally:
        if renew_task is not None:
            renew_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await renew_task
        with contextlib.suppress(Exception):
            await _redis_eval_int(_RELEASE_WEBHOOK_SLOT_LUA, 1, _WEBHOOK_TASK_SLOT_KEY, token)


@asynccontextmanager
async def _webhook_task_slot(*, policy: TaskRuntimePolicy | None = None) -> AsyncIterator[None]:
    runtime_policy = _task_policy(policy)
    limit = runtime_policy.max_concurrent_webhook_tasks
    if limit <= 0:
        yield
        return
    async with _distributed_webhook_task_slot(limit, policy=runtime_policy):
        yield


def _recovery_scan_interval_seconds(policy: TaskRuntimePolicy | None = None) -> int:
    return _task_policy(policy).recovery_scan_interval_seconds


def _metrics_refresh_interval_seconds(policy: TaskRuntimePolicy | None = None) -> int:
    return _task_policy(policy).metrics_refresh_interval_seconds


def _maintenance_cron(policy: TaskRuntimePolicy | None = None) -> str:
    return f"0 {_task_policy(policy).maintenance_hour} * * *"


@asynccontextmanager
async def _scheduled_task_leader(
    name: str, interval_seconds: int, *, policy: TaskRuntimePolicy | None = None
) -> AsyncIterator[bool]:
    """Best-effort singleton guard for scheduled tasks when scheduler is accidentally scaled."""
    key = f"scheduled-task-lock:{name}"
    token = f"{_task_policy(policy).worker_id}:{uuid.uuid4().hex}"
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
        WEBHOOK_RUNNING_TASKS.inc()
        try:
            await handle_webhook_process(event_id=event_id, client_ip=client_ip or "")
        finally:
            WEBHOOK_RUNNING_TASKS.dec()


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
    schedule=[{"interval": _recovery_scan_interval_seconds(), "schedule_id": "openclaw_poll_scan_interval"}],
)
async def scheduled_openclaw_poll_scan() -> None:
    from services.analysis.openclaw_poller import run_openclaw_poll_scan

    await _run_scheduled("openclaw_poll_scan", _recovery_scan_interval_seconds(), run_openclaw_poll_scan())


@broker.task(
    task_name="scheduled_recovery_scan",
    schedule=[{"interval": _recovery_scan_interval_seconds(), "schedule_id": "recovery_scan_interval_seconds"}],
)
async def scheduled_recovery_scan() -> None:
    from services.operations.recovery_poller import run_recovery_scan

    await _run_scheduled("recovery_scan", _recovery_scan_interval_seconds(), run_recovery_scan())


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
    schedule=[{"interval": _recovery_scan_interval_seconds(), "schedule_id": "forward_outbox_scan_interval"}],
)
async def scheduled_forward_outbox_scan() -> None:
    from services.forwarding.outbox import run_forward_outbox_scan

    await _run_scheduled("forward_outbox_scan", _recovery_scan_interval_seconds(), run_forward_outbox_scan())


@broker.task(
    task_name="scheduled_failed_forward_scan",
    schedule=[{"interval": _recovery_scan_interval_seconds(), "schedule_id": "failed_forward_scan_interval"}],
)
async def scheduled_failed_forward_scan() -> None:
    from services.forwarding.retry import run_failed_forward_scan

    await _run_scheduled("failed_forward_scan", _recovery_scan_interval_seconds(), run_failed_forward_scan())


@broker.task(task_name="scheduled_data_maintenance", schedule=[{"cron": _maintenance_cron()}])
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

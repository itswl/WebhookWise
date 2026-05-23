"""Per-alert concurrency gate for webhook analysis.

Redis provides cross-worker single-flight per ``alert_hash``. The in-process
lock is only a local serialization layer after the Redis-backed gate succeeds.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass

from core import redis_client, redis_health
from core.app_context import get_config_manager
from core.logger import get_logger
from core.redis_health import webhook_processing_lock, webhook_processing_queue
from core.redis_lua import (
    ALERT_REFRESH_LOCK_IF_OWNER as _REFRESH_IF_OWNER_LUA,
)
from core.redis_lua import (
    ALERT_RELEASE_LOCK_IF_OWNER as _RELEASE_IF_OWNER_LUA,
)
from core.redis_lua import (
    ALERT_RELEASE_QUEUE_SLOT as _RELEASE_QUEUE_SLOT_LUA,
)
from core.redis_lua import (
    ALERT_RESERVE_QUEUE_SLOT as _RESERVE_QUEUE_SLOT_LUA,
)

logger = get_logger("alert_concurrency")


@dataclass(frozen=True, slots=True)
class AlertProcessingGateResult:
    suppressed: bool
    queue_size: int = 0
    reason: str = ""


@dataclass(frozen=True, slots=True)
class _QueueSlotReservation:
    reserved: bool
    queue_size: int = 0
    suppressed: bool = False
    reason: str = ""


async def _reserve_processing_slot(alert_hash: str) -> _QueueSlotReservation:
    config = get_config_manager()
    threshold = max(0, int(config.retry.PROCESSING_LOCK_FAILFAST_THRESHOLD))
    if not threshold:
        return _QueueSlotReservation(reserved=False, queue_size=0, suppressed=False)

    window_seconds = max(1, int(config.retry.PROCESSING_LOCK_FAILFAST_WINDOW_SECONDS))
    queue_key = webhook_processing_queue(alert_hash)
    if not await redis_health.ensure_redis_available("alert_concurrency:reserve_processing_slot"):
        logger.warning("[Concurrency] Redis 不可用，告警处理槽按背压抑制 alert_hash=%s", alert_hash)
        return _QueueSlotReservation(reserved=False, queue_size=0, suppressed=True, reason="redis_unavailable")

    try:
        queue_size = await redis_client.redis_eval_int(
            _RESERVE_QUEUE_SLOT_LUA,
            1,
            queue_key,
            window_seconds,
            threshold,
        )
    except Exception as e:
        redis_health.mark_redis_failure("alert_concurrency:reserve_processing_slot", e)
        logger.warning("[Concurrency] 告警风暴处理槽预占失败，按背压抑制: %s", e)
        return _QueueSlotReservation(reserved=False, queue_size=0, suppressed=True, reason="redis_unavailable")

    if queue_size is None:
        logger.warning("[Concurrency] 告警风暴处理槽预占返回空结果")
        return _QueueSlotReservation(reserved=False, queue_size=0, suppressed=True, reason="redis_unavailable")

    if queue_size < 0:
        return _QueueSlotReservation(reserved=False, queue_size=abs(queue_size), suppressed=True)
    return _QueueSlotReservation(reserved=True, queue_size=queue_size, suppressed=False)


async def _release_processing_slot(alert_hash: str) -> None:
    config = get_config_manager()
    window_seconds = max(1, int(config.retry.PROCESSING_LOCK_FAILFAST_WINDOW_SECONDS))
    queue_key = webhook_processing_queue(alert_hash)
    try:
        await redis_client.redis_eval_int(_RELEASE_QUEUE_SLOT_LUA, 1, queue_key, window_seconds)
    except Exception as e:
        logger.warning("[Concurrency] 告警风暴处理槽释放失败: %s", e)


def _lock_key(alert_hash: str) -> str:
    return webhook_processing_lock(alert_hash)


async def _acquire_distributed_lock(alert_hash: str) -> tuple[str, str] | None:
    config = get_config_manager()
    if not config.retry.PROCESSING_LOCK_DISTRIBUTED_ENABLED:
        return None

    key = _lock_key(alert_hash)
    token = f"{config.server.WORKER_ID}:{uuid.uuid4().hex}"
    ttl_seconds = max(1, int(config.retry.PROCESSING_LOCK_TTL_SECONDS))
    timeout_seconds = max(0.0, float(config.retry.PROCESSING_LOCK_WAIT_TIMEOUT_SECONDS))
    poll_interval = max(0.01, float(config.retry.PROCESSING_LOCK_POLL_INTERVAL_MS) / 1000.0)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds

    if not await redis_health.ensure_redis_available("alert_concurrency:acquire_distributed_lock"):
        logger.warning("[Concurrency] Redis 不可用，拒绝降级为本进程锁 alert_hash=%s", alert_hash)
        return "", "redis_unavailable"

    while True:
        try:
            if await redis_client.redis_set_nx_ex(key, token, ttl_seconds):
                return key, token
        except Exception as e:
            redis_health.mark_redis_failure("alert_concurrency:acquire_distributed_lock", e)
            logger.warning("[Concurrency] Redis 分布式锁获取失败，按 Redis 不可用抑制: %s", e)
            return "", "redis_unavailable"

        if loop.time() >= deadline:
            return "", ""
        await asyncio.sleep(min(poll_interval, max(0.01, deadline - loop.time())))


async def _release_distributed_lock(key: str, token: str) -> None:
    if not key or not token:
        return
    try:
        await redis_client.redis_eval_int(_RELEASE_IF_OWNER_LUA, 1, key, token)
    except Exception as e:
        logger.warning("[Concurrency] Redis 分布式锁释放失败: %s", e)


async def _refresh_distributed_lock(key: str, token: str, ttl_seconds: int) -> None:
    interval = max(1.0, float(ttl_seconds) / 3.0)
    while True:
        await asyncio.sleep(interval)
        try:
            refreshed = await redis_client.redis_eval_int(_REFRESH_IF_OWNER_LUA, 1, key, token, ttl_seconds)
        except Exception as e:
            logger.warning("[Concurrency] Redis 分布式锁续期失败: %s", e)
            return
        if not refreshed:
            logger.warning("[Concurrency] Redis 分布式锁已失去所有权 key=%s", key)
            return


@asynccontextmanager
async def alert_processing_gate(alert_hash: str) -> AsyncGenerator[AlertProcessingGateResult, None]:
    """Serialize same-alert processing across workers and apply storm backpressure."""
    config = get_config_manager()

    slot = await _reserve_processing_slot(alert_hash)
    if slot.suppressed:
        yield AlertProcessingGateResult(
            suppressed=True,
            queue_size=slot.queue_size,
            reason=slot.reason or "alert_storm_backpressure",
        )
        return

    lock_key: str | None = None
    lock_token: str | None = None
    refresh_task: asyncio.Task[None] | None = None
    try:
        lock = await _acquire_distributed_lock(alert_hash)
        if lock is not None and lock[0] == "":
            yield AlertProcessingGateResult(
                suppressed=True,
                queue_size=slot.queue_size,
                reason=lock[1] or "alert_processing_lock_timeout",
            )
            return
        if lock is not None:
            lock_key, lock_token = lock
            ttl_seconds = max(1, int(config.retry.PROCESSING_LOCK_TTL_SECONDS))
            refresh_task = asyncio.create_task(_refresh_distributed_lock(lock_key, lock_token, ttl_seconds))

        yield AlertProcessingGateResult(suppressed=False, queue_size=slot.queue_size)
    finally:
        if refresh_task:
            refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await refresh_task
        if lock_key and lock_token:
            await _release_distributed_lock(lock_key, lock_token)
        if slot.reserved:
            await _release_processing_slot(alert_hash)

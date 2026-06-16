"""Per-alert concurrency gate for webhook analysis.

Redis provides cross-worker single-flight per ``dedup_key``. The gate must key
on the same value the dedup decision keys on: ``resolve_dedup`` and
``remember_dedup_state`` both operate on ``dedup_key`` (which deliberately
excludes ``severity``), so a flapping alert whose severity oscillates produces
one ``dedup_key`` and must serialise as one alert. Keying the gate on
``alert_hash`` (which includes ``severity``) would let such flaps run
concurrently and each emit a fresh original + forward — exactly the storm case
this gate exists to collapse. The in-process lock is only a local serialization
layer after the Redis-backed gate succeeds.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from redis.exceptions import RedisError

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


class ProcessingLockLost(TimeoutError):
    """Raised when the distributed processing lock was lost mid-flight.

    Subclasses ``TimeoutError`` (an ``OSError``) on purpose: the ingest task's
    retry classifier treats ``OSError`` as retryable, so raising this aborts the
    in-flight commit and re-queues the webhook instead of dead-lettering it. On
    reprocessing, the dedup state written by whichever worker held the lock
    short-circuits to REUSE, so the retry does not duplicate the original.
    """


@dataclass(frozen=True, slots=True)
class AlertProcessingGateResult:
    suppressed: bool
    queue_size: int = 0
    reason: str = ""
    # Set when the distributed lock is lost mid-processing (TTL lapsed or
    # another worker took it). The commit path checks this before persisting
    # side-effects and aborts (raising ProcessingLockLost) if it is set. This
    # narrows the double-processing window but does not fully close it: the lock
    # can still be lost between the check and the commit. None when no
    # distributed lock is held.
    lock_lost: asyncio.Event | None = None


@dataclass(frozen=True, slots=True)
class _QueueSlotReservation:
    reserved: bool
    queue_size: int = 0
    suppressed: bool = False
    reason: str = ""


async def _reserve_processing_slot(dedup_key: str) -> _QueueSlotReservation:
    config = get_config_manager()
    threshold = max(0, int(config.retry.PROCESSING_LOCK_FAILFAST_THRESHOLD))
    if not threshold:
        return _QueueSlotReservation(reserved=False, queue_size=0, suppressed=False)

    window_seconds = max(1, int(config.retry.PROCESSING_LOCK_FAILFAST_WINDOW_SECONDS))
    queue_key = webhook_processing_queue(dedup_key)
    if not await redis_health.ensure_redis_available("alert_concurrency:reserve_processing_slot"):
        logger.warning("[Concurrency] Redis unavailable; alert processing slot suppressed via backpressure dedup_key=%s", dedup_key)
        return _QueueSlotReservation(reserved=False, queue_size=0, suppressed=True, reason="redis_unavailable")

    try:
        queue_size = await redis_client.redis_eval_int(
            _RESERVE_QUEUE_SLOT_LUA,
            1,
            queue_key,
            window_seconds,
            threshold,
        )
    except (RedisError, RuntimeError, TypeError, ValueError) as e:
        redis_health.mark_redis_failure("alert_concurrency:reserve_processing_slot", e)
        logger.warning("[Concurrency] Failed to reserve alert-storm processing slot; suppressing via backpressure: %s", e)
        return _QueueSlotReservation(reserved=False, queue_size=0, suppressed=True, reason="redis_unavailable")

    if queue_size is None:
        logger.warning("[Concurrency] Alert-storm processing slot reservation returned an empty result")
        return _QueueSlotReservation(reserved=False, queue_size=0, suppressed=True, reason="redis_unavailable")

    if queue_size < 0:
        return _QueueSlotReservation(reserved=False, queue_size=abs(queue_size), suppressed=True)
    return _QueueSlotReservation(reserved=True, queue_size=queue_size, suppressed=False)


async def _release_processing_slot(dedup_key: str) -> None:
    config = get_config_manager()
    window_seconds = max(1, int(config.retry.PROCESSING_LOCK_FAILFAST_WINDOW_SECONDS))
    queue_key = webhook_processing_queue(dedup_key)
    try:
        await redis_client.redis_eval_int(_RELEASE_QUEUE_SLOT_LUA, 1, queue_key, window_seconds)
    except (RedisError, RuntimeError, TypeError, ValueError) as e:
        logger.warning("[Concurrency] Failed to release alert-storm processing slot: %s", e)


def _lock_key(dedup_key: str) -> str:
    return webhook_processing_lock(dedup_key)


async def _acquire_distributed_lock(dedup_key: str) -> tuple[str, str] | None:
    config = get_config_manager()
    if not config.retry.PROCESSING_LOCK_DISTRIBUTED_ENABLED:
        return None

    key = _lock_key(dedup_key)
    token = f"{config.server.WORKER_ID}:{uuid.uuid4().hex}"
    ttl_seconds = max(1, int(config.retry.PROCESSING_LOCK_TTL_SECONDS))
    timeout_seconds = max(0.0, float(config.retry.PROCESSING_LOCK_WAIT_TIMEOUT_SECONDS))
    poll_interval = max(0.01, float(config.retry.PROCESSING_LOCK_POLL_INTERVAL_MS) / 1000.0)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds

    if not await redis_health.ensure_redis_available("alert_concurrency:acquire_distributed_lock"):
        logger.warning("[Concurrency] Redis unavailable; refusing to fall back to an in-process lock dedup_key=%s", dedup_key)
        return "", "redis_unavailable"

    while True:
        try:
            if await redis_client.redis_set_nx_ex(key, token, ttl_seconds):
                return key, token
        except (RedisError, RuntimeError, TypeError, ValueError) as e:
            redis_health.mark_redis_failure("alert_concurrency:acquire_distributed_lock", e)
            logger.warning("[Concurrency] Failed to acquire Redis distributed lock; suppressing as Redis-unavailable: %s", e)
            return "", "redis_unavailable"

        if loop.time() >= deadline:
            return "", ""
        await asyncio.sleep(min(poll_interval, max(0.01, deadline - loop.time())))


async def _release_distributed_lock(key: str, token: str) -> None:
    if not key or not token:
        return
    try:
        await redis_client.redis_eval_int(_RELEASE_IF_OWNER_LUA, 1, key, token)
    except (RedisError, RuntimeError, TypeError, ValueError) as e:
        logger.warning("[Concurrency] Failed to release Redis distributed lock: %s", e)


async def _refresh_distributed_lock(
    key: str, token: str, ttl_seconds: int, lock_lost: asyncio.Event | None = None
) -> None:
    interval = max(1.0, float(ttl_seconds) / 3.0)
    while True:
        await asyncio.sleep(interval)
        try:
            refreshed = await redis_client.redis_eval_int(_REFRESH_IF_OWNER_LUA, 1, key, token, ttl_seconds)
        except (RedisError, RuntimeError, TypeError, ValueError) as e:
            logger.warning("[Concurrency] Failed to renew Redis distributed lock; marking lock as possibly lost: %s", e)
            if lock_lost is not None:
                lock_lost.set()
            return
        if not refreshed:
            logger.warning("[Concurrency] Redis distributed lock ownership was lost key=%s", key)
            if lock_lost is not None:
                lock_lost.set()
            return


@asynccontextmanager
async def alert_processing_gate(dedup_key: str) -> AsyncGenerator[AlertProcessingGateResult, None]:
    """Serialize same-dedup-key processing across workers and apply storm backpressure."""
    config = get_config_manager()

    slot = await _reserve_processing_slot(dedup_key)
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
        lock = await _acquire_distributed_lock(dedup_key)
        if lock is not None and lock[0] == "":
            yield AlertProcessingGateResult(
                suppressed=True,
                queue_size=slot.queue_size,
                reason=lock[1] or "alert_processing_lock_timeout",
            )
            return
        lock_lost: asyncio.Event | None = None
        if lock is not None:
            lock_key, lock_token = lock
            ttl_seconds = max(1, int(config.retry.PROCESSING_LOCK_TTL_SECONDS))
            lock_lost = asyncio.Event()
            refresh_task = asyncio.create_task(
                _refresh_distributed_lock(lock_key, lock_token, ttl_seconds, lock_lost)
            )

        yield AlertProcessingGateResult(suppressed=False, queue_size=slot.queue_size, lock_lost=lock_lost)
    finally:
        if refresh_task:
            refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await refresh_task
        if lock_key and lock_token:
            await _release_distributed_lock(lock_key, lock_token)
        if slot.reserved:
            await _release_processing_slot(dedup_key)

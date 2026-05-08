"""Per-alert concurrency gate for webhook analysis.

Redis owns cross-worker single-flight for each ``alert_hash``. A small
in-process keyed lock remains as a local coalescing layer so tasks in the same
worker do not spin on Redis.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from core.logger import logger

_INCR_EXPIRE_IF_FIRST_LUA = """
local c = redis.call("incr", KEYS[1])
if c == 1 then
    redis.call("expire", KEYS[1], tonumber(ARGV[1]))
end
return c
"""

_RELEASE_IF_OWNER_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
end
return 0
"""

_REFRESH_IF_OWNER_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("expire", KEYS[1], tonumber(ARGV[2]))
end
return 0
"""


@dataclass(frozen=True, slots=True)
class AlertProcessingGateResult:
    suppressed: bool
    queue_size: int = 0
    reason: str = ""


@dataclass(slots=True)
class _LockRef:
    lock: asyncio.Lock
    users: int = 0


_lock_refs: dict[str, _LockRef] = {}
_lock_refs_guard = asyncio.Lock()


async def _get_lock_ref(alert_hash: str) -> _LockRef:
    async with _lock_refs_guard:
        ref = _lock_refs.get(alert_hash)
        if ref is None:
            ref = _LockRef(asyncio.Lock())
            _lock_refs[alert_hash] = ref
        ref.users += 1
        return ref


async def _release_lock_ref(alert_hash: str, ref: _LockRef) -> None:
    async with _lock_refs_guard:
        ref.users -= 1
        if ref.users <= 0 and _lock_refs.get(alert_hash) is ref:
            _lock_refs.pop(alert_hash, None)


async def _count_recent_queue_size(alert_hash: str) -> int:
    from core.config import Config
    from core.redis_client import redis_eval_int

    threshold = max(0, int(Config.retry.PROCESSING_LOCK_FAILFAST_THRESHOLD))
    if not threshold:
        return 0

    window_seconds = max(1, int(Config.retry.PROCESSING_LOCK_FAILFAST_WINDOW_SECONDS))
    queue_key = f"queue:webhook:{alert_hash}"
    try:
        return await redis_eval_int(_INCR_EXPIRE_IF_FIRST_LUA, 1, queue_key, window_seconds)
    except Exception as e:
        logger.warning("[Concurrency] 告警风暴计数失败: %s", e)
        return 0


def _lock_key(alert_hash: str) -> str:
    return f"lock:webhook:alert:{alert_hash}"


async def _acquire_distributed_lock(alert_hash: str) -> tuple[str, str] | None:
    from core.config import Config
    from core.redis_client import redis_set_nx_ex

    if not Config.retry.PROCESSING_LOCK_DISTRIBUTED_ENABLED:
        return None

    key = _lock_key(alert_hash)
    token = f"{Config.server.WORKER_ID}:{uuid.uuid4().hex}"
    ttl_seconds = max(1, int(Config.retry.PROCESSING_LOCK_TTL_SECONDS))
    timeout_seconds = max(0.0, float(Config.retry.PROCESSING_LOCK_WAIT_TIMEOUT_SECONDS))
    poll_interval = max(0.01, float(Config.retry.PROCESSING_LOCK_POLL_INTERVAL_MS) / 1000.0)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds

    while True:
        try:
            if await redis_set_nx_ex(key, token, ttl_seconds):
                return key, token
        except Exception as e:
            logger.warning("[Concurrency] Redis 分布式锁获取失败，降级为本进程锁: %s", e)
            return None

        if loop.time() >= deadline:
            return "", ""
        await asyncio.sleep(min(poll_interval, max(0.01, deadline - loop.time())))


async def _release_distributed_lock(key: str, token: str) -> None:
    from core.redis_client import redis_eval_int

    if not key or not token:
        return
    try:
        await redis_eval_int(_RELEASE_IF_OWNER_LUA, 1, key, token)
    except Exception as e:
        logger.warning("[Concurrency] Redis 分布式锁释放失败: %s", e)


async def _refresh_distributed_lock(key: str, token: str, ttl_seconds: int) -> None:
    from core.redis_client import redis_eval_int

    interval = max(1.0, float(ttl_seconds) / 3.0)
    while True:
        await asyncio.sleep(interval)
        try:
            refreshed = await redis_eval_int(_REFRESH_IF_OWNER_LUA, 1, key, token, ttl_seconds)
        except Exception as e:
            logger.warning("[Concurrency] Redis 分布式锁续期失败: %s", e)
            return
        if not refreshed:
            logger.warning("[Concurrency] Redis 分布式锁已失去所有权 key=%s", key)
            return


@asynccontextmanager
async def alert_processing_gate(alert_hash: str) -> AsyncGenerator[AlertProcessingGateResult, None]:
    """Serialize same-alert processing across workers and apply storm backpressure."""
    from core.config import Config

    queue_size = await _count_recent_queue_size(alert_hash)
    threshold = max(0, int(Config.retry.PROCESSING_LOCK_FAILFAST_THRESHOLD))
    suppressed = bool(threshold and queue_size > threshold)
    if suppressed:
        yield AlertProcessingGateResult(
            suppressed=True,
            queue_size=queue_size,
            reason="alert_storm_backpressure",
        )
        return

    ref = await _get_lock_ref(alert_hash)
    await ref.lock.acquire()
    lock_key: str | None = None
    lock_token: str | None = None
    refresh_task: asyncio.Task[None] | None = None
    try:
        lock = await _acquire_distributed_lock(alert_hash)
        if lock == ("", ""):
            yield AlertProcessingGateResult(
                suppressed=True,
                queue_size=queue_size,
                reason="alert_processing_lock_timeout",
            )
            return
        if lock is not None:
            lock_key, lock_token = lock
            ttl_seconds = max(1, int(Config.retry.PROCESSING_LOCK_TTL_SECONDS))
            refresh_task = asyncio.create_task(_refresh_distributed_lock(lock_key, lock_token, ttl_seconds))
        yield AlertProcessingGateResult(suppressed=False, queue_size=queue_size)
    finally:
        if refresh_task:
            refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await refresh_task
        if lock_key and lock_token:
            await _release_distributed_lock(lock_key, lock_token)
        ref.lock.release()
        await _release_lock_ref(alert_hash, ref)

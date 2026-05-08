"""Per-alert concurrency gate for webhook analysis.

Redis is kept only for short-window storm backpressure. Analysis de-duplication
inside a worker process is guarded by an in-memory keyed ``asyncio.Lock``.
"""

from __future__ import annotations

import asyncio
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


@dataclass(frozen=True, slots=True)
class AlertProcessingGateResult:
    suppressed: bool
    queue_size: int = 0


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


@asynccontextmanager
async def alert_processing_gate(alert_hash: str) -> AsyncGenerator[AlertProcessingGateResult, None]:
    """Serialize same-alert analysis inside this process and apply storm backpressure."""
    from core.config import Config

    queue_size = await _count_recent_queue_size(alert_hash)
    threshold = max(0, int(Config.retry.PROCESSING_LOCK_FAILFAST_THRESHOLD))
    suppressed = bool(threshold and queue_size > threshold)
    if suppressed:
        yield AlertProcessingGateResult(suppressed=True, queue_size=queue_size)
        return

    ref = await _get_lock_ref(alert_hash)
    await ref.lock.acquire()
    try:
        yield AlertProcessingGateResult(suppressed=False, queue_size=queue_size)
    finally:
        ref.lock.release()
        await _release_lock_ref(alert_hash, ref)

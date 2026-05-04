"""可复用的 Redis 分布式锁 + Watchdog 自动续期。"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass

from core.logger import logger

# Lua 脚本：仅当 value 匹配时续期锁（原子操作）
_RENEW_LOCK_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("expire", KEYS[1], tonumber(ARGV[2]))
else
    return 0
end
"""

# Lua 脚本：仅当 value 匹配时释放锁（原子操作）
_RELEASE_LOCK_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""

_INCR_EXPIRE_IF_FIRST_LUA = """
local c = redis.call("incr", KEYS[1])
if c == 1 then
    redis.call("expire", KEYS[1], tonumber(ARGV[1]))
end
return c
"""


@dataclass(frozen=True)
class ProcessingLockResult:
    got_lock: bool
    should_wait: bool
    suppressed: bool
    queue_size: int = 0


@asynccontextmanager
async def processing_lock(alert_hash: str) -> AsyncGenerator[ProcessingLockResult, None]:
    """获取基于 alert_hash 的分布式处理锁，集成 Fail-Fast 风暴背压。"""
    from core.config import Config
    from core.redis_client import get_redis

    threshold = max(0, int(Config.retry.PROCESSING_LOCK_FAILFAST_THRESHOLD))
    window_seconds = max(1, int(Config.retry.PROCESSING_LOCK_FAILFAST_WINDOW_SECONDS))
    queue_key, queue_size, suppressed = f"queue:webhook:{alert_hash}", 0, False

    if threshold:
        try:
            redis = get_redis()
            queue_size = int(await redis.eval(_INCR_EXPIRE_IF_FIRST_LUA, 1, queue_key, window_seconds))
            if queue_size > threshold:
                suppressed = True
        except Exception as e:
            logger.warning("processing_lock 计数失败: %s", e)

    lock_key = f"lock:webhook:{alert_hash}"
    lock = DistributedLock(key=lock_key, ttl=Config.retry.PROCESSING_LOCK_TTL_SECONDS)
    lock_acquired = False

    try:
        if not suppressed:
            lock_acquired = await lock.acquire()
            if lock_acquired:
                logger.debug("[Lock] 成功锁定告警: hash=%s", alert_hash)
            else:
                logger.debug("告警正由其他 worker 处理中: hash=%s...", alert_hash[:16])
    except Exception as e:
        logger.error("获取处理锁失败: %s", e)

    try:
        yield ProcessingLockResult(
            got_lock=lock_acquired,
            should_wait=not suppressed and not lock_acquired,
            suppressed=suppressed,
            queue_size=queue_size
        )
    finally:
        await lock.release()
        if lock_acquired:
            logger.debug("释放处理锁: hash=%s...", alert_hash[:16])


class DistributedLock:
    """可复用的 Redis 分布式锁 + Watchdog 自动续期。"""

    def __init__(self, key: str, ttl: int = 60, lock_value: str | None = None) -> None:
        self.key = key
        self.ttl = ttl
        self.value = lock_value or str(uuid.uuid4())
        self._watchdog_task: asyncio.Task | None = None

    async def acquire(self, timeout: float | None = None) -> bool:
        from core.redis_client import get_redis
        redis = get_redis()

        start_time = asyncio.get_event_loop().time()
        while True:
            # SET key value NX EX ttl
            success = await redis.set(self.key, self.value, nx=True, ex=self.ttl)
            if success:
                self._start_watchdog()
                return True

            if timeout is None or (asyncio.get_event_loop().time() - start_time) >= timeout:
                return False

            await asyncio.sleep(0.1)

    async def release(self) -> None:
        from core.redis_client import get_redis
        redis = get_redis()
        self._stop_watchdog()
        with suppress(Exception):
            await redis.eval(_RELEASE_LOCK_LUA, 1, self.key, self.value)

    def _start_watchdog(self) -> None:
        self._stop_watchdog()
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())

    def _stop_watchdog(self) -> None:
        if self._watchdog_task:
            self._watchdog_task.cancel()
            self._watchdog_task = None

    async def _watchdog_loop(self) -> None:
        from core.redis_client import get_redis
        redis = get_redis()
        renew_interval = max(1, self.ttl // 3)
        try:
            while True:
                await asyncio.sleep(renew_interval)
                try:
                    success = await redis.eval(_RENEW_LOCK_LUA, 1, self.key, self.value, self.ttl)
                    if not success:
                        logger.warning("[Lock] Watchdog 续期失败（锁可能已过期或被意外释放）: key=%s", self.key)
                        break
                    logger.debug("[Lock] Watchdog 续期成功: key=%s", self.key)
                except Exception as e:
                    logger.error("[Lock] Watchdog 续期异常: key=%s, err=%s", self.key, e)
                    break
        except asyncio.CancelledError:
            pass

    async def __aenter__(self) -> bool:
        return await self.acquire()

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.release()

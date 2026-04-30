"""可复用的 Redis 分布式锁 + Watchdog 自动续期。

用法（async context manager）::

    async with DistributedLock("lock:key", ttl=120) as acquired:
        if acquired:
            # 持有锁期间执行业务逻辑
            ...

实现细节：
- Redis ``SET NX EX`` 获取锁
- Watchdog 后台协程每 TTL/3 秒自动续期
- Lua 脚本原子校验 value 后续期 / 释放，防止误操作他人锁
"""

from __future__ import annotations

import asyncio
from contextlib import suppress

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


class DistributedLock:
    """可复用的 Redis 分布式锁 + Watchdog 自动续期。

    Parameters:
        key: Redis 锁的 key
        ttl: 锁的过期时间（秒）
        lock_value: 锁的唯一标识值（防误释），默认使用 WORKER_ID
    """

    def __init__(self, key: str, ttl: int = 120, lock_value: str | None = None) -> None:
        self.key = key
        self.ttl = ttl

        if lock_value is None:
            from core.config import Config

            lock_value = Config.server.WORKER_ID
        self.lock_value = lock_value

        self._acquired = False
        self._watchdog_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------

    async def acquire(self) -> bool:
        """尝试获取锁（非阻塞）。

        Returns:
            True 表示成功获取锁，False 表示锁已被其他 Worker 持有。
        """
        import core.redis_client

        redis = core.redis_client.get_redis()
        self._acquired = bool(await redis.set(self.key, self.lock_value, nx=True, ex=self.ttl))
        if self._acquired:
            self._watchdog_task = asyncio.create_task(self._watchdog())
        return self._acquired

    async def release(self) -> None:
        """释放锁：先取消 Watchdog → await 完成 → Lua 原子释放。"""
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._watchdog_task
            self._watchdog_task = None

        if self._acquired:
            try:
                import core.redis_client

                redis = core.redis_client.get_redis()
                await redis.eval(_RELEASE_LOCK_LUA, 1, self.key, self.lock_value)
            except Exception as e:
                logger.error("释放分布式锁失败: key=%s, err=%s", self.key, e)
            finally:
                self._acquired = False

    # ------------------------------------------------------------------
    # Context Manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> bool:
        return await self.acquire()

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:  # noqa: ANN001
        await self.release()

    # ------------------------------------------------------------------
    # 内部：Watchdog 续期
    # ------------------------------------------------------------------

    async def _watchdog(self) -> None:
        """每 TTL/3 秒自动续期锁，在到期前有 2 次续期机会。"""
        import core.redis_client

        interval = self.ttl / 3
        redis = core.redis_client.get_redis()
        try:
            while True:
                await asyncio.sleep(interval)
                try:
                    renewed = await redis.eval(_RENEW_LOCK_LUA, 1, self.key, self.lock_value, str(self.ttl))
                    if not renewed:
                        logger.warning("[Lock] Watchdog 续期失败（锁已不属于自己）: key=%s", self.key)
                        break
                    logger.debug("[Lock] Watchdog 续期成功: key=%s", self.key)
                except Exception as e:
                    logger.error("[Lock] Watchdog 续期异常: key=%s, err=%s", self.key, e)
                    break
        except asyncio.CancelledError:
            pass  # 正常取消

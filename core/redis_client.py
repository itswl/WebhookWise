import asyncio
import contextlib
import threading
from weakref import WeakKeyDictionary

import redis.asyncio as redis

from core.config import Config
from core.logger import logger

# Per-loop Redis 客户端存储，事件循环被 GC 时自动清理
_loop_to_redis: WeakKeyDictionary = WeakKeyDictionary()
_redis_lock = threading.Lock()


def get_redis() -> redis.Redis:
    """获取当前事件循环对应的 Redis 客户端（per-loop 隔离，线程安全）"""
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError as err:
        raise RuntimeError("[Redis] get_redis() 必须在异步上下文中调用") from err

    with _redis_lock:
        if current_loop not in _loop_to_redis:
            pool = redis.ConnectionPool.from_url(
                Config.REDIS_URL,
                decode_responses=True,
                max_connections=100,
            )
            client = redis.Redis(connection_pool=pool)
            _loop_to_redis[current_loop] = client
            logger.info(f"[Redis] 成功初始化连接池: {Config.REDIS_URL}")
        return _loop_to_redis[current_loop]


async def dispose_redis():
    """关闭当前事件循环的 Redis 客户端（应用关闭时调用）"""
    current_loop = asyncio.get_running_loop()
    with _redis_lock:
        client = _loop_to_redis.pop(current_loop, None)
    if client:
        with contextlib.suppress(Exception):
            await client.aclose()
    logger.info("[Redis] 当前连接池已关闭")

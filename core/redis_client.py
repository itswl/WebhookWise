import asyncio
import contextlib
import threading

import redis.asyncio as redis

from core.config import Config
from core.logger import logger

# 全局 Redis 连接池和客户端实例
_redis_pool = None
_redis_client = None
_redis_loop = None  # 记录 Redis 客户端绑定的事件循环
_redis_lock = threading.Lock()


def _create_redis_client() -> redis.Redis:
    """内部方法：创建新的 Redis 连接池和客户端实例"""
    global _redis_pool
    _redis_pool = redis.ConnectionPool.from_url(
        Config.REDIS_URL,
        decode_responses=True,
        max_connections=100
    )
    logger.info(f"[Redis] 成功初始化连接池: {Config.REDIS_URL}")
    return redis.Redis(connection_pool=_redis_pool)


def get_redis() -> redis.Redis:
    """获取全局共享的 Redis 客户端实例（线程安全，自动跟踪事件循环）

    如果当前事件循环与客户端创建时的事件循环不同，
    会自动丢弃旧客户端并创建新客户端，防止跨事件循环使用连接。
    """
    global _redis_pool, _redis_client, _redis_loop

    current_loop = None
    with contextlib.suppress(RuntimeError):
        current_loop = asyncio.get_running_loop()

    with _redis_lock:
        # 如果事件循环发生变化，需要重建客户端和连接池
        if _redis_client is not None and current_loop is not None and _redis_loop is not None and current_loop is not _redis_loop:
            logger.warning("[Redis] 检测到事件循环变更，正在重建 Redis 客户端和连接池")
            # 尝试关闭旧连接池（同步安全方式）
            old_pool = _redis_pool
            _redis_client = None
            _redis_pool = None
            _redis_loop = None
            # 不手动调用 old_pool.disconnect()，因为它是 async 协程，
            # 无法在同步函数中 await。让 GC 自动清理旧连接即可。
            del old_pool

        if _redis_client is None:
            try:
                _redis_client = _create_redis_client()
                _redis_loop = current_loop
            except Exception as e:
                logger.error(f"[Redis] 初始化失败: {e}")
                raise

        return _redis_client


async def dispose_redis():
    """关闭并清理 Redis 连接池（用于应用关闭时调用）"""
    global _redis_pool, _redis_client, _redis_loop
    with _redis_lock:
        if _redis_client is not None:
            logger.info("[Redis] 正在关闭 Redis 连接池")
            try:
                await _redis_client.aclose()
            except Exception as e:
                logger.debug(f"[Redis] 关闭客户端时出错: {e}")
            _redis_client = None
            _redis_pool = None
            _redis_loop = None

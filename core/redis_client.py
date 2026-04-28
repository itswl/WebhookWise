import contextlib

import redis.asyncio as redis

from core.config import Config
from core.logger import logger

_redis_client: redis.Redis | None = None


def get_redis() -> redis.Redis:
    """获取全局 Redis 客户端单例"""
    global _redis_client
    if _redis_client is None:
        pool = redis.ConnectionPool.from_url(
            Config.REDIS_URL,
            decode_responses=True,
            max_connections=100,
        )
        _redis_client = redis.Redis(connection_pool=pool)
        logger.info(f"[Redis] 成功初始化连接池: {Config.REDIS_URL}")
    return _redis_client


async def dispose_redis():
    """关闭 Redis 连接池（应用关闭时调用）"""
    global _redis_client
    if _redis_client:
        with contextlib.suppress(Exception):
            await _redis_client.aclose()
        _redis_client = None
    logger.info("[Redis] 当前连接池已关闭")

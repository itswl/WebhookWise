import redis
from core.config import Config
from core.logger import logger

# 全局 Redis 连接池
_redis_pool = None

def get_redis() -> redis.Redis:
    global _redis_pool
    if _redis_pool is None:
        try:
            _redis_pool = redis.ConnectionPool.from_url(
                Config.REDIS_URL, 
                decode_responses=True,
                max_connections=100
            )
            logger.info(f"成功初始化 Redis 连接池: {Config.REDIS_URL}")
        except Exception as e:
            logger.error(f"初始化 Redis 连接池失败: {e}")
            raise
    return redis.Redis(connection_pool=_redis_pool)

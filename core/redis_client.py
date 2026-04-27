import redis

from core.config import Config
from core.logger import logger

# 全局 Redis 连接池和客户端实例
_redis_pool = None
_redis_client = None

def get_redis() -> redis.Redis:
    """获取全局共享的 Redis 客户端实例（线程安全）"""
    global _redis_pool, _redis_client
    
    if _redis_client is None:
        try:
            # 初始化连接池
            if _redis_pool is None:
                _redis_pool = redis.ConnectionPool.from_url(
                    Config.REDIS_URL, 
                    decode_responses=True,
                    max_connections=100
                )
                logger.info(f"[Redis] 成功初始化连接池: {Config.REDIS_URL}")
            
            # 创建单例客户端
            _redis_client = redis.Redis(connection_pool=_redis_pool)
        except Exception as e: # noqa: PERF203
            logger.error(f"[Redis] 初始化失败: {e}")
            raise
            
    return _redis_client

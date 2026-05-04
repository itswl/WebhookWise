"""TaskIQ Broker 配置 

定义异步任务代理，支持定时任务 (Schedule) 和分布式执行。
"""

import logging

from taskiq import InMemoryBroker, AsyncBroker
from taskiq_redis import RedisAsyncResultBackend, RedisScheduleSource, ListQueueBroker

from core.config import Config

logger = logging.getLogger("webhook_service.taskiq")

# Redis 连接配置
REDIS_URL = Config.redis.REDIS_URL

# 1. 结果后端 (用于获取任务返回值)
result_backend = RedisAsyncResultBackend(
    redis_url=REDIS_URL,
)

# 2. 调度器源 (用于管理定时任务)
schedule_source = RedisScheduleSource(
    redis_url=REDIS_URL,
)

# 3. 异步任务代理 (使用 ListQueueBroker 配合 Redis)
broker = ListQueueBroker(
    url=REDIS_URL,
    result_backend=result_backend,
).with_result_backend(result_backend)

# 在测试环境下可以切换为 InMemoryBroker
if Config.server.DEBUG and not REDIS_URL.startswith("redis"):
    broker = InMemoryBroker()
    logger.info("[TaskIQ] 使用 InMemoryBroker (DEBUG 模式)")
else:
    logger.info(f"[TaskIQ] 已初始化 Redis Broker: {REDIS_URL}")

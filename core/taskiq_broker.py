"""TaskIQ Broker 配置

定义异步任务代理，支持定时任务 (Schedule) 和分布式执行。
"""

import logging

from taskiq import InMemoryBroker
from taskiq_redis import ListQueueBroker, RedisAsyncResultBackend, RedisScheduleSource

from core.config import Config

logger = logging.getLogger("webhook_service.taskiq")

# Redis 连接配置
REDIS_URL = Config.redis.REDIS_URL

# 1. 结果后端 (用于获取任务返回值)
result_backend = RedisAsyncResultBackend(
    redis_url=REDIS_URL,
)

from taskiq.scheduler import TaskiqScheduler

# 2. 调度器源 (用于管理定时任务)
schedule_source = RedisScheduleSource(
    url=REDIS_URL,
)

# 3. 异步任务代理
broker = ListQueueBroker(
    url=REDIS_URL,
    result_backend=result_backend,
).with_result_backend(result_backend)

# 4. 调度器对象
scheduler = TaskiqScheduler(
    broker=broker,
    sources=[schedule_source],
)

# 4. 依赖注入
# TaskIQ 使用 taskiq_dependencies 自动处理依赖
# 不需要显式初始化，但我们需要确保 broker 支持它

# 在测试环境下可以切换为 InMemoryBroker
if Config.server.DEBUG and not REDIS_URL.startswith("redis"):
    broker = InMemoryBroker()
    logger.info("[TaskIQ] 使用 InMemoryBroker (DEBUG 模式)")
else:
    logger.info(f"[TaskIQ] 已初始化 Redis Broker: {REDIS_URL}")

@broker.on_event("startup")
async def startup_event(state: dict):
    """Worker 启动时的生命周期事件"""
    from db.session import init_engine
    from core.http_client import get_http_client
    from core.config import Config
    
    # 确保数据库已初始化
    await init_engine()
    # 确保配置和 HTTP 客户端已初始化
    get_http_client()
    await Config.load_from_db()
    await Config.start_subscriber()

@broker.on_event("shutdown")
async def shutdown_event(state: dict):
    """Worker 关闭时的生命周期事件"""
    from db.session import dispose_engine
    from core.http_client import close_http_client
    from core.config import Config
    
    await Config.stop_subscriber()
    await dispose_engine()
    await close_http_client()

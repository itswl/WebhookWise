"""TaskIQ Broker 配置

定义：
- Broker：供 Worker 消费队列
- Scheduler：独立进程定时投递任务（只负责入队，不执行）
"""

import logging

from taskiq import AsyncBroker, InMemoryBroker, TaskiqEvents, TaskiqScheduler
from taskiq.schedule_sources import LabelScheduleSource
from taskiq_redis import RedisAsyncResultBackend, RedisStreamBroker

from core.config import Config

logger = logging.getLogger("webhook_service.taskiq")

# Redis 连接配置
REDIS_URL = Config.redis.REDIS_URL

# 1. 结果后端
result_backend: RedisAsyncResultBackend[object] = RedisAsyncResultBackend(
    redis_url=REDIS_URL,
)

# 2. 异步任务代理
broker: AsyncBroker = RedisStreamBroker(
    url=REDIS_URL,
    queue_name=Config.server.WEBHOOK_MQ_QUEUE,
    consumer_group_name=Config.server.WEBHOOK_MQ_CONSUMER_GROUP,
    consumer_name=Config.server.WORKER_ID,
    xread_count=Config.server.WEBHOOK_MQ_CONSUMER_BATCH_SIZE,
    xread_block=Config.server.WEBHOOK_MQ_CONSUMER_TIMEOUT_MS,
    maxlen=Config.server.WEBHOOK_MQ_STREAM_MAXLEN,
).with_result_backend(result_backend)

# 在测试环境下可以切换为 InMemoryBroker
if Config.server.DEBUG and not REDIS_URL.startswith("redis"):
    broker = InMemoryBroker()
    logger.info("[TaskIQ] 使用 InMemoryBroker (DEBUG 模式)")
else:
    logger.info("[TaskIQ] 已初始化 Redis Broker: %s", REDIS_URL)

scheduler = TaskiqScheduler(
    broker=broker,
    sources=[LabelScheduleSource(broker)],
)

import services.tasks  # noqa: E402,F401


@broker.on_event(TaskiqEvents.WORKER_STARTUP)
async def worker_startup_event(state: object) -> None:
    """Worker 进程启动时的生命周期事件"""
    from core.config import Config
    from core.http_client import get_http_client
    from core.logger import setup_logger
    from db.session import init_engine

    # 确保日志系统已初始化（taskiq CLI 不走 worker.py::startup）
    setup_logger()
    await init_engine()
    get_http_client()
    if Config.server.ENABLE_RUNTIME_CONFIG:
        await Config.load_from_db()
        await Config.start_subscriber()
    # 初始化 worker 进程 OTEL（TracerProvider + httpx/redis instrumentation）
    from core.otel import setup_otel_worker

    setup_otel_worker()
    # 启动时立即执行一次 recovery，捞起重启前遗留的僵尸事件
    try:
        from services.recovery_poller import run_recovery_scan

        await run_recovery_scan(stuck_threshold_seconds=0)
        logger.info("[TaskIQ] 启动恢复扫描完成")
    except Exception as _e:
        logger.warning("[TaskIQ] 启动恢复扫描失败: %s", _e)


@broker.on_event(TaskiqEvents.WORKER_SHUTDOWN)
async def worker_shutdown_event(state: object) -> None:
    """Worker 进程关闭时的生命周期事件"""
    from core.config import Config
    from core.http_client import close_http_client
    from db.session import dispose_engine

    await Config.stop_subscriber()
    await dispose_engine()
    await close_http_client()

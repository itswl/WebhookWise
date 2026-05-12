"""TaskIQ Broker 配置

定义：
- Broker：供 Worker 消费队列
- Scheduler：独立进程定时投递任务（只负责入队，不执行）
"""

import logging

from taskiq import AsyncBroker, InMemoryBroker, TaskiqEvents, TaskiqScheduler
from taskiq.schedule_sources import LabelScheduleSource
from taskiq_redis import ListRedisScheduleSource, RedisAsyncResultBackend, RedisStreamBroker

from core.config import Config

logger = logging.getLogger("webhook_service.taskiq")

# Redis 连接配置
REDIS_URL = Config.redis.REDIS_URL

# 1. 结果后端
result_backend: RedisAsyncResultBackend[object] = RedisAsyncResultBackend(
    redis_url=REDIS_URL,
)

# 2. 异步任务代理
# RedisStreamBroker uses XREADGROUP with noack=False and exposes XACK to TaskIQ.
# With TaskIQ's default WHEN_SAVED ACK policy, a hard worker crash before result
# persistence leaves the message pending for xautoclaim redelivery. Python task
# exceptions are saved as failed results and ACKed, so domain retries must be
# explicit in task code.
broker: AsyncBroker = RedisStreamBroker(
    url=REDIS_URL,
    queue_name=Config.server.WEBHOOK_MQ_QUEUE,
    consumer_group_name=Config.server.WEBHOOK_MQ_CONSUMER_GROUP,
    consumer_name=Config.server.WORKER_ID,
    xread_count=Config.server.WEBHOOK_MQ_CONSUMER_BATCH_SIZE,
    xread_block=Config.server.WEBHOOK_MQ_CONSUMER_TIMEOUT_MS,
    idle_timeout=Config.server.WEBHOOK_MQ_PENDING_IDLE_TIMEOUT_MS,
    unacknowledged_lock_timeout=max(30.0, Config.server.WEBHOOK_MQ_PENDING_IDLE_TIMEOUT_MS / 1000 * 2),
    maxlen=Config.server.WEBHOOK_MQ_STREAM_MAXLEN,
).with_result_backend(result_backend)

# 在测试环境下可以切换为 InMemoryBroker
if Config.server.DEBUG and not REDIS_URL.startswith("redis"):
    broker = InMemoryBroker()
    logger.info("[TaskIQ] 使用 InMemoryBroker (DEBUG 模式)")
else:
    logger.info("[TaskIQ] 已初始化 Redis Broker: %s", REDIS_URL)

dynamic_schedule_source = ListRedisScheduleSource(
    url=REDIS_URL,
    prefix="taskiq:schedule",
    skip_past_schedules=False,
)

scheduler = TaskiqScheduler(
    broker=broker,
    sources=[LabelScheduleSource(broker), dynamic_schedule_source],
)

import services.operations.tasks  # noqa: E402,F401


@broker.on_event(TaskiqEvents.WORKER_STARTUP)
async def worker_startup_event(state: object) -> None:
    """Worker 进程启动时的生命周期事件"""
    from adapters.ecosystem_adapters import initialize_adapters
    from core.config import Config
    from core.http_client import get_http_client
    from core.logger import setup_logger
    from core.metrics import start_background_metrics_server
    from db.session import init_engine
    from services.analysis.ai_analyzer import initialize_openai_client

    # 确保日志系统已初始化（taskiq CLI 不走 worker.py::startup）
    setup_logger()
    if Config.server.RUN_MODE == "worker":
        start_background_metrics_server()
    initialize_adapters()
    await init_engine()
    get_http_client()
    if Config.server.ENABLE_RUNTIME_CONFIG:
        await Config.load_from_db()
        await Config.start_subscriber()
    if Config.ai.ENABLE_AI_ANALYSIS and Config.ai.OPENAI_API_KEY:
        await initialize_openai_client()
    # 初始化 worker 进程 OTEL（TracerProvider + httpx/redis instrumentation）
    from core.otel import setup_otel_worker

    setup_otel_worker()


@broker.on_event(TaskiqEvents.WORKER_SHUTDOWN)
async def worker_shutdown_event(state: object) -> None:
    """Worker 进程关闭时的生命周期事件"""
    from core.config import Config
    from core.http_client import close_http_client
    from db.session import dispose_engine

    await Config.stop_subscriber()
    await dispose_engine()
    await close_http_client()

"""TaskIQ Broker 配置

定义：
- Broker：供 Worker 消费队列
- Scheduler：独立进程定时投递任务（只负责入队，不执行）
"""

import logging
from dataclasses import dataclass

from taskiq import AsyncBroker, InMemoryBroker, TaskiqEvents, TaskiqScheduler
from taskiq.schedule_sources import LabelScheduleSource
from taskiq_redis import ListRedisScheduleSource, RedisAsyncResultBackend, RedisStreamBroker

from core.config.defaults import get_settings
from core.logging_levels import apply_log_levels

logger = logging.getLogger("webhook_service.taskiq")


@dataclass(frozen=True, slots=True)
class TaskiqBrokerSettings:
    redis_url: str
    queue_name: str
    consumer_group_name: str
    consumer_name: str
    consumer_batch_size: int
    consumer_timeout_ms: int
    pending_idle_timeout_ms: int
    stream_maxlen: int
    debug: bool
    run_mode: str
    log_level: str
    third_party_log_level: str


def load_taskiq_broker_settings() -> TaskiqBrokerSettings:
    settings = get_settings()
    return TaskiqBrokerSettings(
        redis_url=settings.redis.REDIS_URL,
        queue_name=settings.mq.WEBHOOK_MQ_QUEUE,
        consumer_group_name=settings.mq.WEBHOOK_MQ_CONSUMER_GROUP,
        consumer_name=settings.server.WORKER_ID,
        consumer_batch_size=settings.mq.WEBHOOK_MQ_CONSUMER_BATCH_SIZE,
        consumer_timeout_ms=settings.mq.WEBHOOK_MQ_CONSUMER_TIMEOUT_MS,
        pending_idle_timeout_ms=settings.mq.WEBHOOK_MQ_PENDING_IDLE_TIMEOUT_MS,
        stream_maxlen=settings.mq.WEBHOOK_MQ_STREAM_MAXLEN,
        debug=settings.server.DEBUG,
        run_mode=settings.server.RUN_MODE,
        log_level=settings.server.LOG_LEVEL,
        third_party_log_level=settings.server.THIRD_PARTY_LOG_LEVEL,
    )


_settings = load_taskiq_broker_settings()
apply_log_levels(_settings.log_level, _settings.third_party_log_level)

# 1. 结果后端
result_backend: RedisAsyncResultBackend[object] = RedisAsyncResultBackend(
    redis_url=_settings.redis_url,
)

# 2. 异步任务代理
# RedisStreamBroker uses XREADGROUP with noack=False and exposes XACK to TaskIQ.
# With TaskIQ's default WHEN_SAVED ACK policy, a hard worker crash before result
# persistence leaves the message pending for xautoclaim redelivery. Python task
# exceptions are saved as failed results and ACKed, so domain retries must be
# explicit in task code.
broker: AsyncBroker = RedisStreamBroker(
    url=_settings.redis_url,
    queue_name=_settings.queue_name,
    consumer_group_name=_settings.consumer_group_name,
    consumer_name=_settings.consumer_name,
    xread_count=_settings.consumer_batch_size,
    xread_block=_settings.consumer_timeout_ms,
    idle_timeout=_settings.pending_idle_timeout_ms,
    unacknowledged_lock_timeout=max(30.0, _settings.pending_idle_timeout_ms / 1000 * 2),
    maxlen=_settings.stream_maxlen,
).with_result_backend(result_backend)

# 在测试环境下可以切换为 InMemoryBroker
if _settings.debug and not _settings.redis_url.startswith("redis"):
    broker = InMemoryBroker()
    logger.info("[TaskIQ] 使用 InMemoryBroker (DEBUG 模式)")
else:
    logger.info("[TaskIQ] 已初始化 Redis Broker: %s", _settings.redis_url)

dynamic_schedule_source = ListRedisScheduleSource(
    url=_settings.redis_url,
    prefix="taskiq:schedule",
    skip_past_schedules=False,
)

scheduler = TaskiqScheduler(
    broker=broker,
    sources=[LabelScheduleSource(broker), dynamic_schedule_source],
)

if _settings.run_mode == "scheduler":
    from core.observability import setup_observability_scheduler

    setup_observability_scheduler()


@broker.on_event(TaskiqEvents.WORKER_STARTUP)
async def worker_startup_event(state: object) -> None:
    """Worker 进程启动时的生命周期事件"""
    from core.app_context import get_or_create_default_app_context
    from core.logger import setup_logger
    from core.observability import setup_observability_worker
    from core.service_lifecycle import start_runtime_services

    context = get_or_create_default_app_context()
    await start_runtime_services(
        context.config,
        context=context,
        initialize_logger=setup_logger,
        initialize_observability=setup_observability_worker,
        initialize_redis_client=True,
        initialize_ai_client=True,
    )


@broker.on_event(TaskiqEvents.WORKER_SHUTDOWN)
async def worker_shutdown_event(state: object) -> None:
    """Worker 进程关闭时的生命周期事件"""
    from core.app_context import get_or_create_default_app_context
    from core.observability import shutdown_observability
    from core.service_lifecycle import stop_runtime_services

    context = get_or_create_default_app_context()
    await stop_runtime_services(
        context.config,
        context=context,
        reset_ai_client=True,
    )
    shutdown_observability()

"""TaskIQ Broker configuration.

Defines:
- Broker: the queue that Workers consume from.
- Scheduler: a standalone process that periodically dispatches tasks (it only enqueues, it does not execute).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from taskiq import AsyncBroker, InMemoryBroker, TaskiqScheduler
from taskiq.schedule_sources import LabelScheduleSource
from taskiq_redis import ListRedisScheduleSource, RedisAsyncResultBackend, RedisStreamBroker

from core.config.defaults import get_settings
from core.logger import mask_url
from core.logging_levels import apply_log_levels

logger = logging.getLogger("webhook_service.taskiq")


@dataclass(frozen=True, slots=True)
class TaskiqBrokerSettings:
    redis_url: str
    schedule_redis_url: str
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
    worker_startup_jitter_seconds: float
    result_ttl_seconds: int
    schedule_scan_buffer_size: int


def derive_schedule_redis_url(redis_url: str, configured_url: str = "") -> str:
    """Keep schedule scans isolated from task results and application keys."""
    if configured_url.strip():
        return configured_url.strip()
    parsed = urlsplit(redis_url)
    if parsed.scheme not in {"redis", "rediss"}:
        return redis_url
    try:
        database = int(parsed.path.strip("/") or "0")
    except ValueError:
        logger.warning("[TaskIQ] Cannot derive schedule Redis database from %s", mask_url(redis_url))
        return redis_url
    return urlunsplit(parsed._replace(path=f"/{database + 1}"))


def load_taskiq_broker_settings() -> TaskiqBrokerSettings:
    # This module is imported by TaskIQ worker/scheduler bootstrap before the
    # app services are started. Keep it on static settings so broker
    # construction never depends on database-backed application resources.
    settings = get_settings()
    schedule_redis_url = derive_schedule_redis_url(
        settings.redis.REDIS_URL,
        settings.tasks.TASKIQ_SCHEDULE_REDIS_URL,
    )
    return TaskiqBrokerSettings(
        redis_url=settings.redis.REDIS_URL,
        schedule_redis_url=schedule_redis_url,
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
        worker_startup_jitter_seconds=float(settings.tasks.WORKER_STARTUP_JITTER_SECONDS or 0.0),
        result_ttl_seconds=settings.tasks.TASKIQ_RESULT_TTL_SECONDS,
        schedule_scan_buffer_size=settings.tasks.TASKIQ_SCHEDULE_SCAN_BUFFER_SIZE,
    )


_settings = load_taskiq_broker_settings()
apply_log_levels(_settings.log_level, _settings.third_party_log_level)

# 1. Result backend
result_backend: RedisAsyncResultBackend[object] = RedisAsyncResultBackend(
    redis_url=_settings.redis_url,
    result_ex_time=_settings.result_ttl_seconds,
)

# 2. Async task broker
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

# In test environments we can switch to InMemoryBroker
if _settings.debug and not _settings.redis_url.startswith("redis"):
    broker = InMemoryBroker()
    logger.info("[TaskIQ] Using InMemoryBroker (DEBUG mode)")
else:
    logger.info("[TaskIQ] Redis Broker initialized: %s", mask_url(_settings.redis_url))

dynamic_schedule_source = ListRedisScheduleSource(
    url=_settings.schedule_redis_url,
    prefix="taskiq:schedule",
    buffer_size=_settings.schedule_scan_buffer_size,
    skip_past_schedules=False,
)

scheduler = TaskiqScheduler(
    broker=broker,
    sources=[LabelScheduleSource(broker), dynamic_schedule_source],
)

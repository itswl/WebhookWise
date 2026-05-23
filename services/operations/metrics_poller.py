"""系统指标刷新逻辑"""

from __future__ import annotations

from redis.exceptions import RedisError
from sqlalchemy import func, select

from core.logger import get_logger
from core.observability.metrics import (
    DATABASE_EVENTS_COUNT,
    WEBHOOK_MQ_GROUP_LAG,
    WEBHOOK_MQ_GROUP_PENDING,
    WEBHOOK_MQ_STREAM_LENGTH,
    WEBHOOK_PROCESSING_STATUS_COUNT,
)
from core.redis_streams import redis_xinfo_group_lag, redis_xlen, redis_xpending_pending
from db.session import session_scope
from models import WebhookEvent
from core.app_context import get_default_config

logger = get_logger("metrics")


def _default_mq_names() -> tuple[str, str]:
    mq = get_default_config().mq
    return str(mq.WEBHOOK_MQ_QUEUE), str(mq.WEBHOOK_MQ_CONSUMER_GROUP)


async def refresh_all_metrics(*, mq_queue: str | None = None, mq_consumer_group: str | None = None) -> None:
    """刷新系统指标。"""
    await _refresh_db_status_counts()
    await _refresh_mq_stats(mq_queue=mq_queue, mq_consumer_group=mq_consumer_group)
    await _refresh_db_event_count()


async def _refresh_db_event_count() -> None:
    try:
        async with session_scope() as session:
            count = (await session.execute(select(func.count()).select_from(WebhookEvent))).scalar() or 0
        DATABASE_EVENTS_COUNT.set(count)
    except Exception as e:
        logger.debug("[Metrics] 刷新 DB 事件总数失败: %s", e)


async def _refresh_db_status_counts() -> None:
    known_statuses = ("completed", "dead_letter")
    status_counts = dict.fromkeys(known_statuses, 0)

    async with session_scope() as session:
        result = await session.execute(
            select(WebhookEvent.processing_status, func.count()).group_by(WebhookEvent.processing_status)
        )
        for status, count in result.all():
            key = str(status or "")
            if key in status_counts:
                status_counts[key] = int(count or 0)

    for status, count in status_counts.items():
        WEBHOOK_PROCESSING_STATUS_COUNT.labels(status=status).set(count)


async def _refresh_mq_stats(*, mq_queue: str | None = None, mq_consumer_group: str | None = None) -> None:
    """MQ 指标刷新 — TaskIQ 使用 Redis Stream (RedisStreamBroker)。"""
    from core.taskiq_broker import broker

    default_queue, default_group = _default_mq_names()
    queue_name = getattr(broker, "queue_name", None) or mq_queue or default_queue
    group_name = getattr(broker, "consumer_group_name", None) or mq_consumer_group or default_group

    try:
        stream_len = await redis_xlen(queue_name)
        WEBHOOK_MQ_STREAM_LENGTH.labels(stream=queue_name).set(stream_len)
    except RedisError as e:
        logger.debug("[Metrics] 刷新 MQ 队列长度失败: %s", e)

    try:
        pending = await redis_xpending_pending(queue_name, group_name)
        WEBHOOK_MQ_GROUP_PENDING.labels(stream=queue_name, group=group_name).set(pending)

        lag = await redis_xinfo_group_lag(queue_name, group_name)
        WEBHOOK_MQ_GROUP_LAG.labels(stream=queue_name, group=group_name).set(lag)
    except RedisError as e:
        logger.debug("[Metrics] 刷新 MQ group 指标失败: %s", e)

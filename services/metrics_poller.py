"""系统指标刷新逻辑"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from redis.exceptions import RedisError
from sqlalchemy import func, select

from core.config import Config
from core.metrics import (
    DATABASE_EVENTS_COUNT,
    WEBHOOK_MQ_GROUP_LAG,
    WEBHOOK_MQ_GROUP_PENDING,
    WEBHOOK_MQ_STREAM_LENGTH,
    WEBHOOK_PROCESSING_STATUS_COUNT,
    WEBHOOK_STUCK_STATUS_COUNT,
)
from core.redis_client import redis_xinfo_group_lag, redis_xlen, redis_xpending_pending
from db.session import session_scope
from models import WebhookEvent

logger = logging.getLogger("webhook_service.metrics")


async def refresh_all_metrics() -> None:
    """刷新系统指标（由 TaskIQ 驱动）"""
    await _refresh_db_status_counts()
    await _refresh_mq_stats()
    await _refresh_db_event_count()


async def _refresh_db_event_count() -> None:
    try:
        from sqlalchemy import func, select

        from models import WebhookEvent

        async with session_scope() as session:
            count = (await session.execute(select(func.count()).select_from(WebhookEvent))).scalar() or 0
        DATABASE_EVENTS_COUNT.set(count)
    except Exception as e:
        logger.debug("[Metrics] 刷新 DB 事件总数失败: %s", e)


async def _refresh_db_status_counts() -> None:
    known_statuses = ("received", "analyzing", "completed", "failed", "dead_letter")
    status_counts = dict.fromkeys(known_statuses, 0)
    stuck_counts = dict.fromkeys(known_statuses, 0)

    threshold = datetime.now() - timedelta(seconds=Config.server.RECOVERY_POLLER_STUCK_THRESHOLD_SECONDS)

    async with session_scope() as session:
        result = await session.execute(
            select(WebhookEvent.processing_status, func.count()).group_by(WebhookEvent.processing_status)
        )
        for status, count in result.all():
            key = str(status or "")
            if key in status_counts:
                status_counts[key] = int(count or 0)

        result = await session.execute(
            select(WebhookEvent.processing_status, func.count())
            .where(WebhookEvent.processing_status.in_(["received", "analyzing", "failed"]))
            .where(WebhookEvent.created_at < threshold)
            .group_by(WebhookEvent.processing_status)
        )
        for status, count in result.all():
            key = str(status or "")
            if key in stuck_counts:
                stuck_counts[key] = int(count or 0)

    for status, count in status_counts.items():
        WEBHOOK_PROCESSING_STATUS_COUNT.labels(status=status).set(count)
    for status, count in stuck_counts.items():
        WEBHOOK_STUCK_STATUS_COUNT.labels(status=status).set(count)


async def _refresh_mq_stats() -> None:
    """MQ 指标刷新 — TaskIQ 使用 Redis Stream (RedisStreamBroker)。"""
    from core.taskiq_broker import broker

    queue_name = getattr(broker, "queue_name", None) or Config.server.WEBHOOK_MQ_QUEUE
    group_name = getattr(broker, "consumer_group_name", None) or Config.server.WEBHOOK_MQ_CONSUMER_GROUP

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

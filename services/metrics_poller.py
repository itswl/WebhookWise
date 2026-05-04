"""系统指标刷新逻辑"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import func, select

from core.config import Config
from core.metrics import (
    WEBHOOK_MQ_GROUP_LAG,
    WEBHOOK_MQ_GROUP_PENDING,
    WEBHOOK_MQ_STREAM_LENGTH,
    WEBHOOK_PROCESSING_STATUS_COUNT,
    WEBHOOK_STUCK_STATUS_COUNT,
)
from core.redis_client import get_redis
from db.session import session_scope
from models import WebhookEvent

logger = logging.getLogger("webhook_service.metrics")


async def refresh_all_metrics():
    """刷新系统指标（由 TaskIQ 驱动）"""
    await _refresh_db_status_counts()
    await _refresh_mq_stats()


async def _refresh_db_status_counts() -> None:
    known_statuses = ("received", "analyzing", "completed", "failed", "dead_letter")
    status_counts = {s: 0 for s in known_statuses}
    stuck_counts = {s: 0 for s in known_statuses}

    threshold = datetime.now() - timedelta(seconds=Config.server.RECOVERY_POLLER_STUCK_THRESHOLD_SECONDS)

    async with session_scope() as session:
        result = await session.execute(
            select(WebhookEvent.processing_status, func.count())
            .group_by(WebhookEvent.processing_status)
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
    """MQ 指标刷新 (注意：现在迁移到 TaskIQ，这里的 Redis Stream 指标可能不再反映主业务量)"""
    stream = Config.server.WEBHOOK_MQ_QUEUE
    redis = get_redis()

    try:
        stream_len = int(await redis.xlen(stream))
        WEBHOOK_MQ_STREAM_LENGTH.labels(stream=stream).set(stream_len)
    except Exception:
        pass

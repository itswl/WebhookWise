from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timedelta

from sqlalchemy import func, select

from core.config import Config
from core.logger import logger
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


class MetricsPoller:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        self._stop_event.clear()
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "[MetricsPoller] started | interval=%ds",
            Config.server.RECOVERY_POLLER_INTERVAL_SECONDS,
        )

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        logger.info("[MetricsPoller] stopped")

    async def _loop(self) -> None:
        interval = max(5, int(Config.server.RECOVERY_POLLER_INTERVAL_SECONDS))
        while not self._stop_event.is_set():
            try:
                await self._refresh()
            except Exception:
                logger.exception("[MetricsPoller] refresh failed")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
                break
            except asyncio.TimeoutError:
                continue

    async def _refresh(self) -> None:
        await self._refresh_db_status_counts()
        await self._refresh_mq_stats()

    async def _refresh_db_status_counts(self) -> None:
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

    async def _refresh_mq_stats(self) -> None:
        stream = Config.server.WEBHOOK_MQ_QUEUE
        group = Config.server.WEBHOOK_MQ_CONSUMER_GROUP
        redis = get_redis()

        stream_len = 0
        pending = 0
        lag = 0

        try:
            stream_len = int(await redis.xlen(stream))
        except Exception:
            stream_len = 0

        try:
            groups = await redis.xinfo_groups(stream)
            for g in groups or []:
                if str(g.get("name")) != group:
                    continue
                pending = int(g.get("pending", 0) or 0)
                lag = int(g.get("lag", 0) or 0)
                break
        except Exception:
            pending = 0
            lag = 0

        WEBHOOK_MQ_STREAM_LENGTH.labels(stream=stream).set(stream_len)
        WEBHOOK_MQ_GROUP_PENDING.labels(stream=stream, group=group).set(pending)
        WEBHOOK_MQ_GROUP_LAG.labels(stream=stream, group=group).set(lag)

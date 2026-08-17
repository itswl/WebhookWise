"""System metrics refresh logic."""

from __future__ import annotations

from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from core.app_context import get_config_manager
from core.logger import get_logger
from core.observability.metrics import (
    ACTION_CENTER_ACTIVE,
    DATABASE_EVENTS_COUNT,
    WEBHOOK_MQ_GROUP_LAG,
    WEBHOOK_MQ_GROUP_PENDING,
    WEBHOOK_MQ_STREAM_LENGTH,
    WEBHOOK_PROCESSING_STATUS_COUNT,
)
from core.redis_streams import redis_xinfo_group_lag, redis_xlen, redis_xpending_pending
from db.session import count_with_timeout, session_scope
from models import WebhookEvent

logger = get_logger("metrics")


def _default_mq_names() -> tuple[str, str]:
    mq = get_config_manager().mq
    return str(mq.WEBHOOK_MQ_QUEUE), str(mq.WEBHOOK_MQ_CONSUMER_GROUP)


async def refresh_all_metrics(*, mq_queue: str | None = None, mq_consumer_group: str | None = None) -> None:
    """Refresh system metrics."""
    await _refresh_db_status_counts()
    await _refresh_mq_stats(mq_queue=mq_queue, mq_consumer_group=mq_consumer_group)
    await _refresh_db_event_count()
    await _refresh_action_center()
    await _refresh_db_health()


async def _refresh_db_event_count() -> None:
    try:
        async with session_scope() as session:
            count = await count_with_timeout(session, select(func.count()).select_from(WebhookEvent))
            if count is None:
                return
        DATABASE_EVENTS_COUNT.set(count)
    except SQLAlchemyError as e:
        logger.debug("[Metrics] Failed to refresh total DB event count: %s", e)


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
    """Refresh MQ metrics — TaskIQ uses a Redis Stream (RedisStreamBroker)."""
    from core.taskiq_broker import broker

    default_queue, default_group = _default_mq_names()
    queue_name = getattr(broker, "queue_name", None) or mq_queue or default_queue
    group_name = getattr(broker, "consumer_group_name", None) or mq_consumer_group or default_group

    try:
        stream_len = await redis_xlen(queue_name)
        WEBHOOK_MQ_STREAM_LENGTH.labels(stream=queue_name).set(stream_len)
    except RedisError as e:
        logger.debug("[Metrics] Failed to refresh MQ queue length: %s", e)

    try:
        pending = await redis_xpending_pending(queue_name, group_name)
        WEBHOOK_MQ_GROUP_PENDING.labels(stream=queue_name, group=group_name).set(pending)

        lag = await redis_xinfo_group_lag(queue_name, group_name)
        WEBHOOK_MQ_GROUP_LAG.labels(stream=queue_name, group=group_name).set(lag)
    except RedisError as e:
        logger.debug("[Metrics] Failed to refresh MQ group metrics: %s", e)


# Label sets published last time, so a kind that has cleared is set to zero
# rather than left at its last value. A gauge that never comes down would keep
# an alert firing after the fault is fixed, which is how people learn to ignore
# an alert.
_published_action_labels: set[tuple[str, str]] = set()


async def _refresh_action_center() -> None:
    """Publish the action centre's own findings as a metric.

    The detection already exists and is tested; recomputing "what counts as a
    permanent fault" in PromQL would be a second definition to keep in sync.
    """
    global _published_action_labels
    try:
        from services.operations.action_center import get_action_center

        async with session_scope() as session:
            report = await get_action_center(session)
    except Exception as e:  # noqa: BLE001
        # Broader than the other steps on purpose: this one calls a whole
        # subsystem rather than issuing one query, and a metrics refresher must
        # never be the reason the other gauges stop updating.
        logger.debug("[Metrics] Failed to refresh action-centre gauge: %s", e)
        return

    counts: dict[tuple[str, str], int] = {}
    for item in report.get("items") or []:
        key = (str(item.get("kind") or "unknown"), str(item.get("severity") or "unknown"))
        counts[key] = counts.get(key, 0) + 1

    for key in _published_action_labels - set(counts):
        ACTION_CENTER_ACTIVE.labels(*key).set(0)
    for key, count in counts.items():
        ACTION_CENTER_ACTIVE.labels(*key).set(count)
    _published_action_labels = set(counts)


async def _refresh_db_health() -> None:
    """Keep db_health_state alive, so the alert written against it can fire.

    The gauge is set inside test_db_connection(), which runs at startup and on
    the deep health endpoint — neither of which repeats. The series was
    therefore absent from Prometheus entirely, and WebhookWiseDbUnhealthy has
    been loaded, healthy and unable to fire since it was written.
    """
    try:
        from db.engine import test_db_connection

        await test_db_connection()
    except Exception as e:  # noqa: BLE001 - a probe must not break the poller
        logger.debug("[Metrics] DB health probe failed: %s", e)

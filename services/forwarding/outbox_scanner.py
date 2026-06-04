"""Outbox scanner — 定时过期、僵死恢复、backlog 指标。

Called by the scheduled task in services/operations/tasks.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utcnow
from core.logger import get_logger
from core.observability.metrics import (
    FORWARD_OUTBOX_BACKLOG_AGE_SECONDS,
    FORWARD_OUTBOX_RECORDS_TOTAL,
)
from db.session import session_scope
from models import ForwardOutbox
from services.forwarding import outbox_scheduling
from services.forwarding.policies import ForwardDeliveryPolicy
from services.webhooks.types import ForwardOutboxStatus

logger = get_logger("outbox_scanner")


async def _expire_due_outboxes(
    session: AsyncSession, *, now: datetime, policy: ForwardDeliveryPolicy, limit: int
) -> int:
    if policy.max_delivery_age_seconds <= 0 or limit <= 0:
        return 0
    cutoff = now - timedelta(seconds=policy.max_delivery_age_seconds)
    stmt = (
        update(ForwardOutbox)
        .where(
            ForwardOutbox.id.in_(
                select(ForwardOutbox.id)
                .where(
                    ForwardOutbox.status.in_(
                        [ForwardOutboxStatus.PENDING, ForwardOutboxStatus.RETRYING, ForwardOutboxStatus.PROCESSING]
                    )
                )
                .where(ForwardOutbox.created_at < cutoff)
                .order_by(ForwardOutbox.created_at.asc(), ForwardOutbox.id.asc())
                .limit(limit)
            )
        )
        .values(
            status=ForwardOutboxStatus.EXPIRED,
            next_attempt_at=None,
            updated_at=now,
            last_error=f"forward delivery expired after {policy.max_delivery_age_seconds}s",
        )
        .returning(ForwardOutbox)
    )
    expired_records = list((await session.execute(stmt)).scalars().all())
    if expired_records:
        for record in expired_records:
            FORWARD_OUTBOX_RECORDS_TOTAL.labels(str(record.target_type or "unknown"), "expired").inc()
        logger.warning("[OutboxScanner] 批量过期转发意图 count=%s", len(expired_records))
    return len(expired_records)


async def _refresh_outbox_backlog_metrics(session: AsyncSession, *, now: datetime) -> None:
    active_statuses = [
        ForwardOutboxStatus.PENDING,
        ForwardOutboxStatus.RETRYING,
        ForwardOutboxStatus.PROCESSING,
    ]
    rows = (
        await session.execute(
            select(
                ForwardOutbox.target_type,
                ForwardOutbox.status,
                func.min(ForwardOutbox.created_at),
            )
            .where(ForwardOutbox.status.in_(active_statuses))
            .group_by(ForwardOutbox.target_type, ForwardOutbox.status)
        )
    ).all()
    max_age = 0.0
    for target_type, status, oldest_created_at in rows:
        if oldest_created_at is None:
            continue
        status_value = status.value if isinstance(status, ForwardOutboxStatus) else str(status or "unknown")
        age_seconds = max(0.0, (now - oldest_created_at).total_seconds())
        max_age = max(max_age, age_seconds)
        FORWARD_OUTBOX_BACKLOG_AGE_SECONDS.labels(str(target_type or "unknown"), status_value).set(age_seconds)
    FORWARD_OUTBOX_BACKLOG_AGE_SECONDS.labels("all", "active").set(max_age)


async def run_forward_outbox_scan(limit: int = 100, *, policy: ForwardDeliveryPolicy | None = None) -> int:
    """Queue due outbox records and recover stale processing rows."""
    now = utcnow()
    policy = policy or ForwardDeliveryPolicy.from_config()
    stale_before = now - timedelta(seconds=policy.stale_processing_threshold_seconds)
    async with session_scope() as session:
        expired_count = await _expire_due_outboxes(session, now=now, policy=policy, limit=limit)
        await _refresh_outbox_backlog_metrics(session, now=now)
        await session.execute(
            update(ForwardOutbox)
            .where(ForwardOutbox.status == ForwardOutboxStatus.PROCESSING)
            .where(ForwardOutbox.updated_at < stale_before)
            .values(
                status=ForwardOutboxStatus.RETRYING,
                next_attempt_at=now,
                updated_at=now,
                last_error="recovered_stale_processing",
            )
        )
        stmt = (
            select(ForwardOutbox.id)
            .where(ForwardOutbox.status.in_([ForwardOutboxStatus.PENDING, ForwardOutboxStatus.RETRYING]))
            .where((ForwardOutbox.next_attempt_at.is_(None)) | (ForwardOutbox.next_attempt_at <= now))
            .order_by(ForwardOutbox.next_attempt_at.asc(), ForwardOutbox.id.asc())
            .limit(limit)
        )
        ids = list((await session.execute(stmt)).scalars().all())
    await outbox_scheduling.schedule_forward_outbox_many(ids)
    FORWARD_OUTBOX_RECORDS_TOTAL.labels("unknown", "scan_queued").inc(len(ids))
    if expired_count:
        FORWARD_OUTBOX_RECORDS_TOTAL.labels("unknown", "scan_expired").inc(expired_count)
    return expired_count + len(ids)

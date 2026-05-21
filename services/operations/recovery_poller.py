"""Recovery logic for legacy DB-backed webhook rows."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select, update

from core.observability.metrics import WEBHOOK_RECOVERY_POLLED_TOTAL
from db.session import session_scope
from models import WebhookEvent
from services.operations.policies import RecoveryScanPolicy
from services.webhooks.types import WebhookProcessingStatus

logger = logging.getLogger("webhook_service.recovery")

# 每次最多处理的僵尸事件数量
_MAX_RECOVER_BATCH = 50


def _stale_event_condition(threshold: datetime) -> Any:
    return and_(
        WebhookEvent.processing_status.in_([WebhookProcessingStatus.RECEIVED, WebhookProcessingStatus.ANALYZING]),
        func.coalesce(WebhookEvent.updated_at, WebhookEvent.created_at) < threshold,
    )


def _event_age_expr() -> Any:
    return func.coalesce(WebhookEvent.updated_at, WebhookEvent.created_at)


async def run_recovery_scan(
    stuck_threshold_seconds: int | None = None, *, policy: RecoveryScanPolicy | None = None
) -> None:
    """扫描旧版 DB 事件路径中真正卡住的记录并重新处理。

    raw ingest 主路径不会在分析前写入 ``webhook_events``。这里仅兜底
    legacy event_id 重放、手动 replay、worker 崩溃后遗留的 received/analyzing/retry 行。
    """
    policy = policy or RecoveryScanPolicy.from_config(
        stuck_threshold_seconds=stuck_threshold_seconds,
        batch_size=_MAX_RECOVER_BATCH,
    )
    threshold_secs = policy.stuck_threshold_seconds
    now = datetime.now()
    threshold = now - timedelta(seconds=threshold_secs)
    retry_next_at = now + timedelta(seconds=policy.scan_interval_seconds)
    logger.debug(
        "[Recovery] 开始扫描 threshold_secs=%s batch_size=%s max_retries=%s",
        threshold_secs,
        policy.batch_size,
        policy.max_retries,
    )

    recovered_ids = await _claim_recoverable_events(
        now=now,
        threshold=threshold,
        retry_next_at=retry_next_at,
        max_retries=policy.max_retries,
        limit=policy.batch_size,
    )

    if not recovered_ids:
        logger.debug("[Recovery] 本轮没有可恢复事件 threshold_secs=%s", threshold_secs)
        return

    logger.info("[Recovery] 发现 %d 条僵尸事件，开始恢复处理", len(recovered_ids))

    await _enqueue_recovered_events(recovered_ids)

    logger.info("[Recovery] 本轮恢复完成 recovered=%d threshold_secs=%d", len(recovered_ids), threshold_secs)


async def _claim_recoverable_events(
    *,
    now: datetime,
    threshold: datetime,
    retry_next_at: datetime,
    max_retries: int,
    limit: int,
) -> list[int]:
    """Atomically claim recoverable rows and return event ids to enqueue."""
    if limit <= 0:
        return []
    async with session_scope() as session:
        retry_ids_subq = (
            select(WebhookEvent.id)
            .where(WebhookEvent.processing_status == WebhookProcessingStatus.RETRY)
            .where(WebhookEvent.retry_count < max_retries)
            .where(or_(WebhookEvent.next_retry_at.is_(None), WebhookEvent.next_retry_at <= now))
            .order_by(WebhookEvent.next_retry_at.asc().nullsfirst(), WebhookEvent.id.asc())
            .limit(limit)
            .subquery()
        )
        retry_res = await session.execute(
            update(WebhookEvent)
            .where(WebhookEvent.id.in_(select(retry_ids_subq.c.id)))
            .where(WebhookEvent.processing_status == WebhookProcessingStatus.RETRY)
            .where(WebhookEvent.retry_count < max_retries)
            .where(or_(WebhookEvent.next_retry_at.is_(None), WebhookEvent.next_retry_at <= now))
            .values(
                next_retry_at=retry_next_at,
                failure_reason="retry_recovered",
                error_message=f"retry requeued by recovery at {now.isoformat(timespec='seconds')}",
                updated_at=now,
            )
            .returning(WebhookEvent.id)
        )
        retry_ids = [int(row[0]) for row in retry_res.all()]
        remaining = limit - len(retry_ids)
        if remaining <= 0:
            return retry_ids

        stale_ids_subq = (
            select(WebhookEvent.id)
            .where(_stale_event_condition(threshold))
            .where(WebhookEvent.retry_count < max_retries)
            .order_by(_event_age_expr().asc(), WebhookEvent.id.asc())
            .limit(remaining)
            .subquery()
        )
        stale_res = await session.execute(
            update(WebhookEvent)
            .where(WebhookEvent.id.in_(select(stale_ids_subq.c.id)))
            .where(_stale_event_condition(threshold))
            .where(WebhookEvent.retry_count < max_retries)
            .values(
                processing_status=WebhookProcessingStatus.RETRY,
                retry_count=WebhookEvent.retry_count + 1,
                next_retry_at=retry_next_at,
                failure_reason="stuck_recovery",
                error_message=f"recovered_by_poller at {now.isoformat(timespec='seconds')}",
                updated_at=now,
            )
            .returning(WebhookEvent.id)
        )
        stale_ids = [int(row[0]) for row in stale_res.all()]
        return retry_ids + stale_ids


async def _enqueue_recovered_events(event_ids: list[int]) -> None:
    """Enqueue claimed events independently so one TaskIQ failure does not stop the batch."""
    from services.operations.tasks import process_webhook_task

    for event_id in event_ids:
        try:
            logger.info("[Recovery] 已重新入队 event_id=%s", event_id)
            await process_webhook_task.kiq(event_id=event_id, client_ip="recovery")
        except Exception:
            logger.exception("[Recovery] 恢复事件 %s 入队失败", event_id)
            continue
        WEBHOOK_RECOVERY_POLLED_TOTAL.inc()

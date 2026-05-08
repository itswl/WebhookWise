"""Recovery逻辑 — 扫描僵尸事件并重新分发。"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import or_, select, update

from core.config import Config
from core.metrics import WEBHOOK_RECOVERY_POLLED_TOTAL
from db.session import session_scope
from models import WebhookEvent

logger = logging.getLogger("webhook_service.recovery")

# 每次最多处理的僵尸事件数量
_MAX_RECOVER_BATCH = 50


async def run_recovery_scan(stuck_threshold_seconds: int | None = None) -> None:
    """扫描真正卡住的事件并重新处理（由 TaskIQ 驱动，不再自启动循环）。

    常规可重试失败由 TaskIQ 延迟调度推进；这里仅兜底 worker 崩溃、
    入队后未消费等导致长期停留在 received/analyzing 的事件。
    """
    threshold_secs = (
        stuck_threshold_seconds
        if stuck_threshold_seconds is not None
        else Config.server.RECOVERY_POLLER_STUCK_THRESHOLD_SECONDS
    )
    now = datetime.now()
    threshold = now - timedelta(seconds=threshold_secs)

    async with session_scope() as session:
        result = await session.execute(
            select(WebhookEvent)
            .where(WebhookEvent.processing_status.in_(["received", "analyzing"]))
            .where(WebhookEvent.retry_count < Config.retry.WEBHOOK_RETRY_MAX_RETRIES)
            .where(or_(WebhookEvent.updated_at < threshold, WebhookEvent.created_at < threshold))
            .limit(_MAX_RECOVER_BATCH)
        )
        zombie_events = result.scalars().all()

    if not zombie_events:
        return

    logger.info("[Recovery] 发现 %d 条僵尸事件，开始恢复处理", len(zombie_events))

    for e in zombie_events:
        await _recover_single_event(e)

    logger.info("[Recovery] 本轮恢复完成 recovered=%d threshold_secs=%d", len(zombie_events), threshold_secs)


async def _recover_single_event(e: WebhookEvent) -> None:
    """恢复单条事件，独立 try-except 避免影响循环"""
    try:
        from services.tasks import process_webhook_task

        async with session_scope() as session:
            stmt = (
                update(WebhookEvent)
                .where(WebhookEvent.id == e.id)
                .where(WebhookEvent.processing_status.in_(["received", "analyzing"]))
                .where(WebhookEvent.retry_count < Config.retry.WEBHOOK_RETRY_MAX_RETRIES)
                .values(
                    processing_status="retry",
                    retry_count=WebhookEvent.retry_count + 1,
                    failure_reason="stuck_recovery",
                    error_message=f"recovered_by_poller at {datetime.now().isoformat(timespec='seconds')}",
                    updated_at=datetime.now(),
                )
                .returning(WebhookEvent.id, WebhookEvent.retry_count)
            )
            res = await session.execute(stmt)
            row = res.first()
            if not row:
                return

        logger.info("[Recovery] 已重新入队 event_id=%s", e.id)
        WEBHOOK_RECOVERY_POLLED_TOTAL.inc()
        await process_webhook_task.kiq(event_id=e.id, client_ip="recovery")
    except Exception:
        logger.exception("[Recovery] 恢复事件 %s 失败", e.id)

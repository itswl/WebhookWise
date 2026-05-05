"""Recovery逻辑 — 扫描僵尸事件并重新分发。"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select

from core.config import Config
from core.metrics import WEBHOOK_RECOVERY_POLLED_TOTAL
from db.session import session_scope
from models import WebhookEvent

logger = logging.getLogger("webhook_service.recovery")

# 每次最多处理的僵尸事件数量
_MAX_RECOVER_BATCH = 50
# 最大重试次数
_MAX_RETRIES = 5


async def run_recovery_scan(stuck_threshold_seconds: int | None = None):
    """扫描僵尸事件并重新处理（由 TaskIQ 驱动，不再自启动循环）"""
    threshold_secs = stuck_threshold_seconds if stuck_threshold_seconds is not None else Config.server.RECOVERY_POLLER_STUCK_THRESHOLD_SECONDS
    threshold = datetime.now() - timedelta(seconds=threshold_secs)

    async with session_scope() as session:
        result = await session.execute(
            select(WebhookEvent)
            .where(WebhookEvent.processing_status.in_(["received", "analyzing", "failed"]))
            .where(WebhookEvent.retry_count < _MAX_RETRIES)
            .where(WebhookEvent.created_at < threshold)
            .limit(_MAX_RECOVER_BATCH)
        )
        zombie_events = result.scalars().all()

    if not zombie_events:
        return

    logger.info("[Recovery] 发现 %d 条僵尸事件，开始恢复处理", len(zombie_events))

    for e in zombie_events:
        await _recover_single_event(e)


async def _recover_single_event(e: WebhookEvent):
    """恢复单条事件，独立 try-except 避免影响循环"""
    try:
        from services.pipeline import handle_webhook_process
        logger.info("[Recovery] 重新处理事件 id=%s", e.id)
        WEBHOOK_RECOVERY_POLLED_TOTAL.inc()
        await handle_webhook_process(event_id=e.id, client_ip="recovery")
    except Exception:
        logger.exception("[Recovery] 恢复事件 %s 失败", e.id)

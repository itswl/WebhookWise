"""僵尸事件恢复 — 捞取停滞的 webhook 事件并重新处理，实现 At-Least-Once Delivery"""

import json
import logging
from datetime import datetime, timedelta

from sqlalchemy import select

from db.session import session_scope
from models import WebhookEvent

logger = logging.getLogger(__name__)

# 每次最多处理的僵尸事件数量
_MAX_RECOVER_BATCH = 50

# 超过此时间仍处于 received/analyzing 的事件视为僵尸
_ZOMBIE_THRESHOLD_MINUTES = 5


async def recover_zombie_events() -> None:
    """捞取停滞的 webhook 事件并重新处理"""
    threshold = datetime.utcnow() - timedelta(minutes=_ZOMBIE_THRESHOLD_MINUTES)

    async with session_scope() as session:
        result = await session.execute(
            select(WebhookEvent)
            .where(WebhookEvent.processing_status.in_(["received", "analyzing"]))
            .where(WebhookEvent.created_at < threshold)
            .limit(_MAX_RECOVER_BATCH)
        )
        zombie_events = result.scalars().all()

    if not zombie_events:
        return

    logger.info(f"[Recovery] 发现 {len(zombie_events)} 条僵尸事件，开始恢复处理")

    for event in zombie_events:
        try:
            await _reprocess_event(event)
        except Exception:  # noqa: PERF203
            logger.exception(f"[Recovery] 恢复事件 {event.id} 失败")


async def _reprocess_event(event: WebhookEvent) -> None:
    """重新处理单条僵尸事件"""
    from services.pipeline import handle_webhook_process

    raw_payload = event.raw_payload or ""
    raw_body = raw_payload.encode("utf-8")
    headers = event.headers if isinstance(event.headers, dict) else {}
    source = event.source or "unknown"

    try:
        payload = json.loads(raw_payload) if raw_payload else {}
    except (json.JSONDecodeError, TypeError):
        payload = {}

    logger.info(f"[Recovery] 重新处理事件 id={event.id}, source={source}")

    await handle_webhook_process(
        client_ip="recovery",
        headers=headers,
        payload=payload,
        raw_body=raw_body,
        source=source,
        event_id=event.id,
    )

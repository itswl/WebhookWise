"""Feishu/Lark notification transport."""

from __future__ import annotations

from typing import Any

from core.logger import get_logger
from services.channels.feishu import build_deep_analysis_card
from services.forwarding.enqueue import enqueue_external_message

logger = get_logger("feishu_notifications")


async def send_feishu_deep_analysis(
    webhook_url: str,
    analysis_record: dict[str, Any],
    source: str = "",
    webhook_event_id: int = 0,
    *,
    timeout_seconds: int | None = None,
    http_client: Any | None = None,
    channels: Any | None = None,
) -> bool:
    """Send a deep-analysis card to a configured Feishu/Lark webhook."""
    if not webhook_url:
        return False
    try:
        payload = build_deep_analysis_card(analysis_record, source=source, webhook_event_id=webhook_event_id)
        await enqueue_external_message(
            channel_name="feishu",
            target_url=webhook_url,
            event_type="deep_analysis",
            formatted_payload=payload,
            webhook_id=webhook_event_id or None,
            idempotency_hint=f"deep_analysis:{analysis_record.get('engine', '')}:{webhook_event_id}",
        )
        return True
    except Exception as e:
        logger.warning("深度分析通知入队失败: %s", e)
        return False

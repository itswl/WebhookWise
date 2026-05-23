"""Feishu/Lark notification transport."""

from __future__ import annotations

from typing import Any

from core.logger import get_logger
from services.notifications import (
    AsyncJsonPoster,
    NotificationChannel,
    build_notification_channels,
    find_notification_channel,
)

logger = get_logger("feishu_notifications")


async def send_feishu_deep_analysis(
    webhook_url: str,
    analysis_record: dict[str, Any],
    source: str = "",
    webhook_event_id: int = 0,
    *,
    timeout_seconds: int | None = None,
    http_client: AsyncJsonPoster | None = None,
    channels: list[NotificationChannel] | None = None,
) -> bool:
    """Send a deep-analysis card to a configured Feishu/Lark webhook."""
    if not webhook_url:
        return False
    channels = channels or build_notification_channels(http_client=http_client, timeout_seconds=timeout_seconds)
    channel = find_notification_channel(webhook_url, channels)
    if channel is None:
        logger.debug("未找到支持该 URL 的通知渠道: %s", webhook_url)
        return False
    return await channel.send_deep_analysis(
        webhook_url,
        analysis_record,
        source=source,
        webhook_event_id=webhook_event_id,
    )

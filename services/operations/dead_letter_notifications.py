"""Dead-letter operational notifications.

This module owns the infrastructure-specific notification details so webhook
processing does not need to know which chat product receives the alert.
"""

from core.logger import logger
from services.notifications.channels import AsyncJsonPoster
from services.notifications.factory import build_notification_channels, find_notification_channel
from services.operations.policies import DeadLetterNotificationPolicy, FeishuNotificationPolicy


async def notify_dead_letter(
    event_id: int,
    retry_count: int,
    error: Exception,
    *,
    target_url: str | None = None,
    http_client: AsyncJsonPoster | None = None,
    policy: DeadLetterNotificationPolicy | None = None,
) -> None:
    """Send a best-effort dead-letter notification to the configured ops target."""
    try:
        policy = policy or DeadLetterNotificationPolicy.from_config()
        url = target_url if target_url is not None else policy.target_url
        if not url:
            return
        channels = build_notification_channels(
            http_client=http_client,
            feishu_policy=FeishuNotificationPolicy(timeout_seconds=10),
        )
        channel = find_notification_channel(url, channels)
        if channel is None:
            logger.debug("[DeadLetter] configured notification target has no matching channel")
            return

        from adapters.plugins.feishu_card import build_dead_letter_card

        await channel.send_card(url, build_dead_letter_card(event_id, retry_count, error))
    except Exception as e:
        logger.warning("[DeadLetter] 发送死信告警失败: %s", e)

"""Operational notifications for AI analysis errors."""

import hashlib
import logging
from typing import Any

from adapters.plugins.feishu_card import build_ai_error_card
from services.analysis.ai_policies import AIErrorNotificationPolicy
from services.notifications.factory import build_notification_channels, find_notification_channel
from services.operations.policies import FeishuNotificationPolicy
from services.webhooks.types import WebhookData

logger = logging.getLogger("webhook_service.ai_error_notifications")


async def send_ai_error_alert(
    webhook_data: WebhookData,
    error_reason: str,
    *,
    is_degraded: bool = False,
    policy: AIErrorNotificationPolicy | None = None,
    http_client: Any | None = None,
) -> None:
    """Send a rate-limited AI error/degradation alert to the configured operations target."""
    policy = policy or AIErrorNotificationPolicy.from_config()
    if not policy.enabled or not policy.target_url:
        return

    try:
        from core.redis_client import redis_set_nx_ex

        error_hash = hashlib.md5(error_reason[:100].encode("utf-8"), usedforsecurity=False).hexdigest()[:8]
        lock_key = f"ai_error_alert_lock:{error_hash}"
        if not await redis_set_nx_ex(lock_key, "1", policy.cooldown_seconds):
            return

        channels = build_notification_channels(
            http_client=http_client,
            feishu_policy=FeishuNotificationPolicy(timeout_seconds=policy.timeout_seconds),
        )
        channel = find_notification_channel(policy.target_url, channels)
        if channel is None:
            logger.debug("AI 错误通知目标没有匹配的通知渠道: %s", policy.target_url)
            return
        await channel.send_card(
            policy.target_url,
            build_ai_error_card(webhook_data, error_reason, is_degraded=is_degraded),
        )
    except Exception as e:
        logger.error("发送 AI 错误通知失败: %s", e)

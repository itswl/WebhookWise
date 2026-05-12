"""Operational notifications for AI analysis errors."""

import hashlib
import logging
from typing import Any

from adapters.plugins.feishu_card import build_ai_error_card
from core.circuit_breaker import CircuitBreakerOpenException, feishu_cb
from core.http_client import get_http_client
from core.url_security import validate_outbound_url
from services.analysis.ai_policies import AIErrorNotificationPolicy
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

        target_url = await validate_outbound_url(policy.target_url)
        client = http_client or get_http_client()
        await feishu_cb.call_async(
            client.post,
            target_url,
            json=build_ai_error_card(webhook_data, error_reason, is_degraded=is_degraded),
            timeout=policy.timeout_seconds,
        )
    except CircuitBreakerOpenException as e:
        logger.warning("发送 AI 错误通知被熔断器拦截: %s", e)
    except Exception as e:
        logger.error("发送 AI 错误通知失败: %s", e)

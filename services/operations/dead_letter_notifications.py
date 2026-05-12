"""Dead-letter operational notifications.

This module owns the infrastructure-specific notification details so webhook
processing does not need to know which chat product receives the alert.
"""

from typing import Protocol

from adapters.notification_targets import is_feishu_url
from core.http_client import get_http_client
from core.logger import logger
from core.url_security import validate_outbound_url
from services.operations.policies import DeadLetterNotificationPolicy


class AsyncJsonPoster(Protocol):
    async def post(self, url: str, *, json: object, timeout: float | int | None = None) -> object: ...


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
        url = await validate_outbound_url(url)
        if not is_feishu_url(url):
            logger.debug("[DeadLetter] configured notification target is not Feishu/Lark, skip card alert")
            return

        from adapters.plugins.feishu_card import build_dead_letter_card

        client = http_client or get_http_client()
        await client.post(url, json=build_dead_letter_card(event_id, retry_count, error), timeout=10)
    except Exception as e:
        logger.warning("[DeadLetter] 发送死信告警失败: %s", e)

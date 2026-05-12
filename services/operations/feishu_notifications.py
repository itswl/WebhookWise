"""Feishu/Lark notification transport helpers for operational workflows."""

from __future__ import annotations

import logging
from typing import Any, Protocol

from adapters.plugins.feishu_card import build_deep_analysis_card
from core.circuit_breaker import CircuitBreakerOpenException, feishu_cb
from core.http_client import get_http_client
from core.url_security import validate_outbound_url
from services.operations.policies import FeishuNotificationPolicy

logger = logging.getLogger("webhook_service.feishu_notifications")


class AsyncJsonPoster(Protocol):
    async def post(self, url: str, *, json: object, timeout: float | int | None = None) -> object: ...


async def send_feishu_deep_analysis(
    webhook_url: str,
    analysis_record: dict[str, Any],
    source: str = "",
    webhook_event_id: int = 0,
    *,
    policy: FeishuNotificationPolicy | None = None,
    http_client: AsyncJsonPoster | None = None,
) -> bool:
    """Send a deep-analysis card to a configured Feishu/Lark webhook."""
    if not webhook_url:
        return False
    policy = policy or FeishuNotificationPolicy.from_config()
    try:
        target_url = await validate_outbound_url(webhook_url)
        client = http_client or get_http_client()
        response = await feishu_cb.call_async(
            client.post,
            target_url,
            json=build_deep_analysis_card(analysis_record, source=source, webhook_event_id=webhook_event_id),
            timeout=policy.timeout_seconds,
        )
    except CircuitBreakerOpenException as e:
        logger.warning("飞书深度分析通知被熔断器拦截: %s", e)
        return False
    except Exception as e:
        logger.warning("飞书深度分析通知发送失败: %s", e)
        return False
    return getattr(response, "status_code", None) == 200

"""Feishu/Lark notification channel implementation."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from adapters.notification_targets import is_feishu_url
from adapters.plugins.feishu_card import build_deep_analysis_card
from core.circuit_breaker import CircuitBreaker, CircuitBreakerOpenException
from core.url_security import validate_outbound_url
from services.forwarding.dependencies import ValidateURL
from services.notifications.channels import AsyncJsonPoster
from services.operations.policies import FeishuNotificationPolicy

logger = logging.getLogger("webhook_service.notifications.feishu")


@dataclass(frozen=True, slots=True)
class FeishuNotificationChannel:
    http_client: AsyncJsonPoster
    circuit_breaker: CircuitBreaker
    policy: FeishuNotificationPolicy
    validate_url: ValidateURL = validate_outbound_url
    name: str = "feishu"

    def supports(self, target_url: str) -> bool:
        return is_feishu_url(target_url)

    async def send_card(self, target_url: str, card_payload: object) -> bool:
        if not target_url or not self.supports(target_url):
            return False
        try:
            validated_url = await self.validate_url(target_url)
            response = await self.circuit_breaker.call_async(
                self.http_client.post,
                validated_url,
                json=card_payload,
                timeout=self.policy.timeout_seconds,
            )
        except CircuitBreakerOpenException as e:
            logger.warning("飞书通知被熔断器拦截: %s", e)
            return False
        except Exception as e:
            logger.warning("飞书通知发送失败: %s", e)
            return False
        return getattr(response, "status_code", None) == 200

    async def send_deep_analysis(
        self,
        target_url: str,
        analysis_record: dict[str, Any],
        *,
        source: str = "",
        webhook_event_id: int = 0,
    ) -> bool:
        return await self.send_card(
            target_url,
            build_deep_analysis_card(analysis_record, source=source, webhook_event_id=webhook_event_id),
        )

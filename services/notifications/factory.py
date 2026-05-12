"""Notification channel construction helpers."""

from __future__ import annotations

from core.circuit_breaker import feishu_cb
from core.http_client import get_http_client
from core.url_security import validate_outbound_url
from services.forwarding.dependencies import ValidateURL
from services.notifications.channels import AsyncJsonPoster, NotificationChannel
from services.notifications.feishu import FeishuNotificationChannel
from services.operations.policies import FeishuNotificationPolicy


def build_notification_channels(
    *,
    http_client: AsyncJsonPoster | None = None,
    feishu_policy: FeishuNotificationPolicy | None = None,
    validate_url: ValidateURL | None = None,
) -> list[NotificationChannel]:
    client = http_client or get_http_client()
    return [
        FeishuNotificationChannel(
            http_client=client,
            circuit_breaker=feishu_cb,
            policy=feishu_policy or FeishuNotificationPolicy.from_config(),
            validate_url=validate_url or validate_outbound_url,
        )
    ]


def find_notification_channel(target_url: str, channels: list[NotificationChannel]) -> NotificationChannel | None:
    for channel in channels:
        if channel.supports(target_url):
            return channel
    return None

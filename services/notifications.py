"""Notification channel abstractions and implementations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

from adapters.plugins.feishu_card import build_deep_analysis_card
from core.circuit_breaker import CircuitBreakerOpenException
from core.app_context import get_default_config
from core.http_client import get_http_client
from core.logger import get_logger, mask_url
from core.observability.tracing import span as otel_span
from core.url_security import validate_outbound_url
from services.forwarding.circuit_breakers import feishu_cb
from services.forwarding.dependencies import CircuitBreakerLike, ValidateURL


class AsyncJsonPoster(Protocol):
    async def post(self, url: str, *, json: object, timeout: float | int | None = None) -> object: ...


class NotificationChannel(Protocol):
    @property
    def name(self) -> str: ...

    def supports(self, target_url: str) -> bool: ...

    async def send_card(self, target_url: str, card_payload: object) -> bool: ...

    async def send_deep_analysis(
        self,
        target_url: str,
        analysis_record: dict[str, Any],
        *,
        source: str = "",
        webhook_event_id: int = 0,
    ) -> bool: ...


_FEISHU_HOST_SUFFIXES = (".feishu.cn", ".larksuite.com")
_FEISHU_HOSTS = ("feishu.cn", "larksuite.com")


def is_feishu_url(url: str) -> bool:
    try:
        host = (urlsplit(str(url)).hostname or "").lower().rstrip(".")
    except Exception:
        return False
    return host in _FEISHU_HOSTS or any(host.endswith(suffix) for suffix in _FEISHU_HOST_SUFFIXES)


logger = get_logger("notifications.feishu")


@dataclass(frozen=True, slots=True)
class FeishuNotificationChannel:
    http_client: AsyncJsonPoster
    circuit_breaker: CircuitBreakerLike
    timeout_seconds: int
    validate_url: ValidateURL = validate_outbound_url
    name: str = "feishu"

    def supports(self, target_url: str) -> bool:
        return is_feishu_url(target_url)

    async def send_card(self, target_url: str, card_payload: object) -> bool:
        if not target_url or not self.supports(target_url):
            return False
        with otel_span("notification.send", {"forward.target": self.name}):
            try:
                validated_url = await self.validate_url(target_url)
                logger.info("[FeishuNotify] 开始发送卡片 target=%s", mask_url(validated_url))

                async def _do_post() -> Any:
                    final_url = await self.validate_url(validated_url)
                    return await self.http_client.post(
                        final_url,
                        json=card_payload,
                        timeout=self.timeout_seconds,
                    )

                response = await self.circuit_breaker.call_async(_do_post)
            except CircuitBreakerOpenException as e:
                logger.warning("[FeishuNotify] 发送被熔断器拦截 target=%s error=%s", mask_url(target_url), e)
                return False
            except Exception as e:
                logger.warning(
                    "[FeishuNotify] 发送失败 target=%s error_type=%s error=%s",
                    mask_url(target_url),
                    type(e).__name__,
                    e,
                )
                return False
            success = getattr(response, "status_code", None) == 200
            logger.info(
                "[FeishuNotify] 发送完成 target=%s status_code=%s success=%s",
                mask_url(target_url),
                getattr(response, "status_code", None),
                success,
            )
            return success

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


def build_notification_channels(
    *,
    http_client: AsyncJsonPoster | None = None,
    timeout_seconds: int | None = None,
    validate_url: ValidateURL | None = None,
) -> list[NotificationChannel]:
    client = http_client or get_http_client()
    resolved_timeout = int(get_default_config().notifications.FEISHU_WEBHOOK_TIMEOUT) if timeout_seconds is None else int(timeout_seconds)
    return [
        FeishuNotificationChannel(
            http_client=client,
            circuit_breaker=feishu_cb,
            timeout_seconds=max(1, resolved_timeout),
            validate_url=validate_url or validate_outbound_url,
        )
    ]


def find_notification_channel(target_url: str, channels: list[NotificationChannel]) -> NotificationChannel | None:
    for channel in channels:
        if channel.supports(target_url):
            return channel
    return None

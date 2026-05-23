from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from services.webhooks.types import AnalysisResult, ForwardResult, WebhookData


@dataclass(frozen=True, slots=True)
class FormatContext:
    webhook_data: WebhookData
    analysis_result: AnalysisResult
    is_periodic_reminder: bool = False


class WebhookChannel:
    name = "webhook"

    def format(self, ctx: FormatContext) -> dict[str, Any]:
        return {
            "webhook": ctx.webhook_data,
            "analysis": ctx.analysis_result,
            "is_periodic_reminder": ctx.is_periodic_reminder,
        }

    async def send(self, url: str, payload: dict[str, Any]) -> ForwardResult:
        from services.forwarding.policies import ForwardDeliveryPolicy
        from services.forwarding.remote import post_json_to_remote

        return await post_json_to_remote(
            url,
            payload,
            policy=ForwardDeliveryPolicy.from_config(),
            validate_target=True,
            target_type_label=self.name,
        )


_feishu_channel: Any = None
_webhook_channel: WebhookChannel | None = None


def _get_feishu_channel() -> Any:
    global _feishu_channel
    if _feishu_channel is None:
        from services.channels.feishu import FeishuChannel

        _feishu_channel = FeishuChannel()
    return _feishu_channel


def _get_webhook_channel() -> WebhookChannel:
    global _webhook_channel
    if _webhook_channel is None:
        _webhook_channel = WebhookChannel()
    return _webhook_channel


def resolve_channel(target_type: str, target_url: str) -> Any:
    """根据 target_type 或 URL 解析 Channel 实例。"""
    if target_type == "feishu":
        return _get_feishu_channel()
    if target_type == "openclaw":
        return None  # openclaw handled in deliver_outbox_record
    from services.channels.feishu import is_feishu_url

    if is_feishu_url(target_url):
        return _get_feishu_channel()
    return _get_webhook_channel()

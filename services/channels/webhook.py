from __future__ import annotations

from typing import Any

from services.channels.base import FormatContext, SendResult
from services.forwarding.policies import RemoteForwardPolicy


class WebhookChannel:
    name = "webhook"

    def supports(self, target_url: str) -> bool:
        return True

    def format(self, ctx: FormatContext) -> dict[str, Any]:
        return {
            "webhook": ctx.webhook_data,
            "analysis": ctx.analysis_result,
            "is_periodic_reminder": ctx.is_periodic_reminder,
        }

    async def send(self, url: str, payload: dict[str, Any]) -> SendResult:
        from services.forwarding.remote import post_json_to_remote

        return await post_json_to_remote(
            url,
            payload,
            policy=RemoteForwardPolicy.from_config(),
            validate_target=True,
            target_type_label=self.name,
        )

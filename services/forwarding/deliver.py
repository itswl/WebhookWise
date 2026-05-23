from __future__ import annotations

from typing import Any, cast

from models import ForwardOutbox
from services.channels.base import get_channel, resolve_channel_name
from services.webhooks.types import AnalysisResult, ForwardResult, WebhookData


async def deliver_outbox_record(record: ForwardOutbox) -> ForwardResult:
    channel_name = str(record.channel_name or record.target_type or "")
    if channel_name == "openclaw":
        from services.forwarding.openclaw import forward_to_openclaw

        forward_data = cast(WebhookData, dict(record.forward_data or {}))
        analysis = cast(AnalysisResult, dict(record.analysis_result or {}))
        return await forward_to_openclaw(forward_data, analysis)

    resolved_name = resolve_channel_name(channel_name, str(record.target_url or ""))
    channel = get_channel(resolved_name)
    if channel is None:
        return {"status": "failed", "message": f"unknown_channel:{resolved_name}"}
    payload = record.formatted_payload
    if not isinstance(payload, dict):
        payload = None
    if payload is None and isinstance(record.forward_data, dict) and isinstance(record.analysis_result, dict):
        from services.channels.base import FormatContext

        payload = channel.format(
            FormatContext(
                webhook_data=cast(WebhookData, dict(record.forward_data)),
                analysis_result=cast(AnalysisResult, dict(record.analysis_result)),
                is_periodic_reminder=bool(record.is_periodic_reminder),
            )
        )
    if payload is None:
        payload = {}
    return await channel.send(str(record.target_url or ""), cast(dict[str, Any], payload))

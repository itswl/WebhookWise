"""Delivery execution for forwarding outbox records."""

from __future__ import annotations

from typing import cast

from contracts.webhook_payload import JsonObject, WebhookData
from models import ForwardOutbox
from services.analysis import openclaw_analysis
from services.forwarding import circuit_breakers, remote
from services.notifications import feishu
from services.webhooks.types import AnalysisResult, ForwardResult, is_pending_result


def _is_forward_success(result: ForwardResult) -> bool:
    return result.get("status") == "success" or is_pending_result(result)


async def deliver_outbox_record(record: ForwardOutbox) -> ForwardResult:
    channel_name = str(record.channel_name or record.target_type or "")
    if channel_name == "openclaw":
        forward_data = cast(WebhookData, dict(record.forward_data or {}))
        analysis = cast(AnalysisResult, dict(record.analysis_result or {}))
        return await openclaw_analysis.forward_to_openclaw(forward_data, analysis)

    target_url = str(record.target_url or "")
    payload: JsonObject | None = record.formatted_payload if isinstance(record.formatted_payload, dict) else None
    if payload is None and isinstance(record.forward_data, dict) and isinstance(record.analysis_result, dict):
        webhook_data = cast(WebhookData, dict(record.forward_data))
        analysis_result = cast(AnalysisResult, dict(record.analysis_result))
        is_reminder = bool(record.is_periodic_reminder)
        if feishu.is_feishu_url(target_url):
            payload = feishu.build_feishu_card(webhook_data, analysis_result, is_periodic_reminder=is_reminder)
        else:
            payload = {"webhook": webhook_data, "analysis": analysis_result, "is_periodic_reminder": is_reminder}
    if payload is None:
        payload = {}

    if feishu.is_feishu_url(target_url):
        return await feishu.send_to_feishu(target_url, payload)

    deps = circuit_breakers.build_remote_forward_dependencies(target_url)
    return await remote.post_json_to_remote(
        target_url,
        payload,
        dependencies=deps,
        target_type_label=channel_name or "webhook",
    )

"""Forward-channel strategy registry.

Replaces the hardcoded if/elif on target_type / feishu-URL spread across
outbox_delivery, remote and outbox_state with a small registry of channel
strategies. Adding a new forward target type means adding one ForwardChannel,
not editing several dispatch sites.

A channel owns:
- name: stable identifier used for metrics labels.
- matches(record): whether this channel handles a given outbox record.
- build_payload(record): the JSON body to deliver (None when the channel builds
  its own payload at delivery time).
- deliver(record, payload): perform the external send, returning a ForwardResult.
- needs_deep_analysis_record / deep_analysis_fields: post-commit success hook so
  the outbox state machine can persist follow-up rows (OpenClaw) without
  knowing channel specifics.
"""

from __future__ import annotations

from typing import Protocol, cast, runtime_checkable

from contracts.webhook_payload import JsonObject, WebhookData
from models import ForwardOutbox
from services.webhooks.types import AnalysisResult, ForwardResult, is_pending_result


@runtime_checkable
class ForwardChannel(Protocol):
    name: str

    def matches(self, record: ForwardOutbox) -> bool: ...

    async def deliver(self, record: ForwardOutbox) -> ForwardResult: ...

    def needs_followup_on_success(self, record: ForwardOutbox, result: ForwardResult) -> bool: ...


class _OpenClawChannel:
    name = "openclaw"

    def matches(self, record: ForwardOutbox) -> bool:
        return str(record.channel_name or record.target_type or "") == "openclaw"

    async def deliver(self, record: ForwardOutbox) -> ForwardResult:
        from services.analysis import openclaw_analysis

        forward_data = cast(WebhookData, dict(record.forward_data or {}))
        analysis = cast(AnalysisResult, dict(record.analysis_result or {}))
        return await openclaw_analysis.forward_to_openclaw(forward_data, analysis)

    def needs_followup_on_success(self, record: ForwardOutbox, result: ForwardResult) -> bool:
        # A pending OpenClaw trigger spawns a DeepAnalysis poll record.
        return is_pending_result(result)


def _build_http_payload(record: ForwardOutbox) -> JsonObject:
    """Payload for HTTP channels (feishu card / generic webhook envelope)."""
    from services.notifications import feishu

    payload = record.formatted_payload if isinstance(record.formatted_payload, dict) else None
    if payload is not None:
        return payload
    if isinstance(record.forward_data, dict) and isinstance(record.analysis_result, dict):
        webhook_data = cast(WebhookData, dict(record.forward_data))
        analysis_result = cast(AnalysisResult, dict(record.analysis_result))
        is_reminder = bool(record.is_periodic_reminder)
        if feishu.is_feishu_url(str(record.target_url or "")):
            return feishu.build_feishu_card(webhook_data, analysis_result, is_periodic_reminder=is_reminder)
        return {"webhook": webhook_data, "analysis": analysis_result, "is_periodic_reminder": is_reminder}
    return {}


class _FeishuChannel:
    name = "feishu"

    def matches(self, record: ForwardOutbox) -> bool:
        from services.notifications import feishu

        if str(record.channel_name or record.target_type or "") == "openclaw":
            return False
        return feishu.is_feishu_url(str(record.target_url or ""))

    async def deliver(self, record: ForwardOutbox) -> ForwardResult:
        from services.notifications import feishu

        return await feishu.send_to_feishu(str(record.target_url or ""), _build_http_payload(record))

    def needs_followup_on_success(self, record: ForwardOutbox, result: ForwardResult) -> bool:
        return False


class _WebhookChannel:
    """Generic HTTP webhook target — the catch-all fallback."""

    name = "webhook"

    def matches(self, record: ForwardOutbox) -> bool:
        return True  # fallback; registry tries this last

    async def deliver(self, record: ForwardOutbox) -> ForwardResult:
        from services.forwarding import circuit_breakers, remote

        target_url = str(record.target_url or "")
        deps = circuit_breakers.build_remote_forward_dependencies(target_url)
        label = str(record.channel_name or record.target_type or "") or "webhook"
        return await remote.post_json_to_remote(
            target_url,
            _build_http_payload(record),
            dependencies=deps,
            target_type_label=label,
        )

    def needs_followup_on_success(self, record: ForwardOutbox, result: ForwardResult) -> bool:
        return False


# Order matters: openclaw and feishu are specific, webhook is the fallback.
_CHANNELS: tuple[ForwardChannel, ...] = (_OpenClawChannel(), _FeishuChannel(), _WebhookChannel())


def resolve_channel(record: ForwardOutbox) -> ForwardChannel:
    for channel in _CHANNELS:
        if channel.matches(record):
            return channel
    return _CHANNELS[-1]  # _WebhookChannel fallback (matches() is always True anyway)

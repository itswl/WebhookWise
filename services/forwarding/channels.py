"""Forward-channel strategy registry.

Replaces the hardcoded if/elif on target_type / feishu-URL that used to be
spread across the outbox delivery/state code with a small registry of channel
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

from typing import Any, Protocol, cast, runtime_checkable

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


def _build_http_payload(record: ForwardOutbox, *, kb_links: list[dict[str, str]] | None = None) -> JsonObject:
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
            return feishu.build_feishu_card(
                webhook_data, analysis_result, is_periodic_reminder=is_reminder, kb_links=kb_links
            )
        return {"webhook": webhook_data, "analysis": analysis_result, "is_periodic_reminder": is_reminder}
    return {}


async def _kb_links_for_alert_card(record: ForwardOutbox) -> list[dict[str, str]] | None:
    """Matching published KB snippets for an alert-card delivery; None to omit.

    Only the freshly-built alert card path gets a KB block (pre-formatted
    payloads are system cards). Best-effort: any failure delivers the card
    without KB rather than delaying it.
    """
    if isinstance(record.formatted_payload, dict):
        return None
    if not isinstance(record.forward_data, dict) or not isinstance(record.analysis_result, dict):
        return None
    try:
        from core.app_context import get_config_manager
        from services.kb.card_links import find_kb_snippets_for_alert

        kb_cfg = get_config_manager().kb
        if not bool(kb_cfg.KB_CARD_LINKS_ENABLED):
            return None
        parsed_obj = record.forward_data.get("parsed_data") or record.forward_data.get("body") or {}
        parsed = parsed_obj if isinstance(parsed_obj, dict) else {}
        rule_name = str(parsed.get("RuleName") or parsed.get("AlertName") or parsed.get("alert_name") or "")
        links = await find_kb_snippets_for_alert(
            source=str(record.forward_data.get("source") or ""),
            rule_name=rule_name,
            summary=str(record.analysis_result.get("summary") or ""),
            limit=int(kb_cfg.KB_CARD_LINKS_MAX),
        )
        return links or None
    except (KeyError, RuntimeError, TypeError, ValueError) as e:
        from core.logger import get_logger

        get_logger("forwarding.channels").warning("[Forward] KB card-link lookup failed (omitting block): %s", e)
        return None


class _FeishuChannel:
    name = "feishu"

    def matches(self, record: ForwardOutbox) -> bool:
        from services.notifications import feishu

        if str(record.channel_name or record.target_type or "") == "openclaw":
            return False
        return feishu.is_feishu_url(str(record.target_url or ""))

    async def deliver(self, record: ForwardOutbox) -> ForwardResult:
        from services.notifications import feishu

        kb_links = await _kb_links_for_alert_card(record)
        return await feishu.send_to_feishu(
            str(record.target_url or ""),
            _build_http_payload(record, kb_links=kb_links),
            idempotency_key=str(record.idempotency_key or "") or None,
        )

    def needs_followup_on_success(self, record: ForwardOutbox, result: ForwardResult) -> bool:
        return False


def _build_bot_markdown_payload(record: ForwardOutbox, builder: Any) -> JsonObject:
    """Payload for markdown bot channels (DingTalk / WeCom).

    Alert forwards carry forward_data + analysis and get a native markdown
    message. A pre-formatted payload (system cards are built for Feishu) or a
    record without analysis falls back to the payload as-is — the bot will
    reject non-conforming JSON visibly rather than dropping it silently.
    """
    if isinstance(record.forward_data, dict) and isinstance(record.analysis_result, dict):
        webhook_data = cast(WebhookData, dict(record.forward_data))
        analysis_result = cast(AnalysisResult, dict(record.analysis_result))
        return cast(
            JsonObject,
            builder(webhook_data, analysis_result, is_periodic_reminder=bool(record.is_periodic_reminder)),
        )
    return _build_http_payload(record)


class _DingTalkChannel:
    """DingTalk robot target, selected by its webhook URL."""

    name = "dingtalk"

    def matches(self, record: ForwardOutbox) -> bool:
        from services.notifications import dingtalk

        return dingtalk.is_dingtalk_url(str(record.target_url or ""))

    async def deliver(self, record: ForwardOutbox) -> ForwardResult:
        from services.forwarding import circuit_breakers, remote
        from services.notifications import dingtalk

        target_url = str(record.target_url or "")
        return await remote.post_json_to_remote(
            target_url,
            _build_bot_markdown_payload(record, dingtalk.build_dingtalk_markdown),
            dependencies=circuit_breakers.build_remote_forward_dependencies(target_url),
            target_type_label=self.name,
            idempotency_key=str(record.idempotency_key or "") or None,
        )

    def needs_followup_on_success(self, record: ForwardOutbox, result: ForwardResult) -> bool:
        return False


class _WeComChannel:
    """WeCom (企业微信) group-bot target, selected by its webhook URL."""

    name = "wecom"

    def matches(self, record: ForwardOutbox) -> bool:
        from services.notifications import wecom

        return wecom.is_wecom_url(str(record.target_url or ""))

    async def deliver(self, record: ForwardOutbox) -> ForwardResult:
        from services.forwarding import circuit_breakers, remote
        from services.notifications import wecom

        target_url = str(record.target_url or "")
        return await remote.post_json_to_remote(
            target_url,
            _build_bot_markdown_payload(record, wecom.build_wecom_markdown),
            dependencies=circuit_breakers.build_remote_forward_dependencies(target_url),
            target_type_label=self.name,
            idempotency_key=str(record.idempotency_key or "") or None,
        )

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
            idempotency_key=str(record.idempotency_key or "") or None,
        )

    def needs_followup_on_success(self, record: ForwardOutbox, result: ForwardResult) -> bool:
        return False


# Order matters: openclaw, feishu, dingtalk, and wecom are specific (the last
# three keyed on their bot URL shapes); webhook is the fallback.
_CHANNELS: tuple[ForwardChannel, ...] = (
    _OpenClawChannel(),
    _FeishuChannel(),
    _DingTalkChannel(),
    _WeComChannel(),
    _WebhookChannel(),
)


def resolve_channel(record: ForwardOutbox) -> ForwardChannel:
    for channel in _CHANNELS:
        if channel.matches(record):
            return channel
    return _CHANNELS[-1]  # _WebhookChannel fallback (matches() is always True anyway)

"""Notification-to-outbox enqueue helpers."""

from __future__ import annotations

from typing import Any

from core.logger import get_logger
from db.session import session_scope
from services.forwarding import rules as forwarding_rules
from services.forwarding.outbox_records import create_outbox_records, outbox_result
from services.forwarding.outbox_scheduling import schedule_forward_outbox_many
from services.forwarding.policies import ForwardDeliveryPolicy
from services.webhooks.decisioning import ForwardRuleSnapshot, select_forward_rules
from services.webhooks.types import AnalysisResult, ForwardResult

logger = get_logger("forward_outbox")


async def create_forward_notification_outbox_records(
    *,
    event_type: str,
    source: str = "",
    formatted_payload: dict[str, Any] | None = None,
    forward_data: dict[str, Any] | None = None,
    analysis_result: AnalysisResult | None = None,
    webhook_id: int | None = None,
    policy: ForwardDeliveryPolicy,
    target_url: str = "",
    idempotency_extra: str = "",
    importance: str = "",
    is_duplicate: bool = False,
    parsed_data: dict[str, Any] | None = None,
) -> tuple[list[int], str]:
    if target_url:
        matched = [
            ForwardRuleSnapshot(
                id=None,
                name="manual_forward",
                match_event_type="",
                match_importance="",
                match_source="",
                match_duplicate="",
                match_payload="",
                target_type="webhook",
                target_url=target_url,
                stop_on_match=True,
                target_name="",
            )
        ]
    else:
        rules = await forwarding_rules.list_enabled_forward_rules()
        matched = select_forward_rules(
            rules,
            event_type=event_type,
            importance=importance,
            source=source,
            is_duplicate=is_duplicate,
            parsed_data=parsed_data,
        )
    if not matched:
        logger.info("[ForwardNotify] 无匹配规则 event_type=%s source=%s", event_type, source)
        return [], "未匹配转发规则" if not target_url else "目标 URL 为空"

    async with session_scope() as sess:
        outbox_ids = await create_outbox_records(
            sess,
            matched,
            webhook_id=webhook_id,
            orig_id=None,
            forward_data=forward_data,
            analysis_result=analysis_result,
            formatted_payload=formatted_payload,
            event_type=event_type,
            is_periodic_reminder=False,
            idempotency_extra=idempotency_extra,
            policy=policy,
            log_tag="ForwardNotify",
        )
    return outbox_ids, ""


async def enqueue_forward_notification(
    *,
    event_type: str,
    source: str = "",
    formatted_payload: dict[str, Any] | None = None,
    forward_data: dict[str, Any] | None = None,
    analysis_result: AnalysisResult | None = None,
    webhook_id: int | None = None,
    policy: ForwardDeliveryPolicy | None = None,
    target_url: str = "",
    idempotency_extra: str = "",
    importance: str = "",
    is_duplicate: bool = False,
    parsed_data: dict[str, Any] | None = None,
) -> ForwardResult:
    policy = policy or ForwardDeliveryPolicy.from_config()
    outbox_ids, skip_reason = await create_forward_notification_outbox_records(
        event_type=event_type,
        source=source,
        formatted_payload=formatted_payload,
        forward_data=forward_data,
        analysis_result=analysis_result,
        webhook_id=webhook_id,
        policy=policy,
        target_url=target_url,
        idempotency_extra=idempotency_extra,
        importance=importance,
        is_duplicate=is_duplicate,
        parsed_data=parsed_data,
    )
    if skip_reason:
        return {"status": "skipped", "reason": skip_reason, "outbox_ids": []}
    if outbox_ids:
        await schedule_forward_outbox_many(outbox_ids)
    return outbox_result(outbox_ids)

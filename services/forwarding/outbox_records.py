"""Low-level forwarding outbox record creation helpers."""

from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utcnow
from core.logger import get_logger
from core.observability.metrics import FORWARD_OUTBOX_RECORDS_TOTAL
from models import ForwardOutbox
from services.forwarding.policies import ForwardDeliveryPolicy
from services.webhooks.decisioning import ForwardRuleSnapshot
from services.webhooks.types import (
    AnalysisResult,
    ForwardOutboxStatus,
    ForwardResult,
)

logger = get_logger("forward_outbox")


async def create_outbox_records(
    session: AsyncSession,
    matched_rules: list[ForwardRuleSnapshot],
    *,
    webhook_id: int | None,
    orig_id: int | None,
    forward_data: dict[str, Any] | None,
    analysis_result: AnalysisResult | None,
    formatted_payload: dict[str, Any] | None,
    event_type: str,
    is_periodic_reminder: bool,
    idempotency_extra: str = "",
    policy: ForwardDeliveryPolicy,
    log_tag: str,
) -> list[int]:
    """Create outbox records for matched rules within an existing session."""
    now = utcnow()
    outbox_ids: list[int] = []
    for rule in matched_rules:
        target_type = str(rule.target_type or "webhook")
        target_url = str(rule.target_url or "")
        if target_type != "openclaw" and not target_url:
            logger.warning("[%s] 规则 '%s' target_url 为空，跳过", log_tag, rule.name or rule.id)
            FORWARD_OUTBOX_RECORDS_TOTAL.labels(target_type, "skipped_empty_target").inc()
            continue

        rule_id = rule.id
        key = idempotency_key(
            webhook_id=webhook_id or 0,
            rule_id=rule_id,
            target_type=target_type,
            target_url=target_url,
            is_periodic_reminder=is_periodic_reminder,
            extra=idempotency_extra,
        )
        existing = (
            await session.execute(select(ForwardOutbox.id).where(ForwardOutbox.idempotency_key == key))
        ).scalar_one_or_none()
        if existing is not None:
            logger.info("[%s] 幂等命中 key=%s id=%s", log_tag, key, existing)
            FORWARD_OUTBOX_RECORDS_TOTAL.labels(target_type, "duplicate").inc()
            outbox_ids.append(int(existing))
            continue

        record = ForwardOutbox(
            idempotency_key=key,
            webhook_event_id=webhook_id,
            original_event_id=orig_id,
            forward_rule_id=rule_id,
            rule_name=str(rule.name or rule.id or "default"),
            target_type=target_type,
            target_url=target_url,
            target_name=str(rule.target_name or ""),
            is_periodic_reminder=is_periodic_reminder,
            channel_name=target_type,
            event_type=event_type,
            status=ForwardOutboxStatus.PENDING,
            attempts=0,
            max_attempts=policy.max_attempts,
            next_attempt_at=now,
            forward_data=forward_data,
            analysis_result=analysis_result,
            formatted_payload=formatted_payload,
            created_at=now,
            updated_at=now,
        )
        session.add(record)
        await session.flush()
        outbox_ids.append(int(record.id))
        FORWARD_OUTBOX_RECORDS_TOTAL.labels(target_type, "created").inc()
        logger.info(
            "[%s] 已创建转发意图 id=%s event_id=%s event_type=%s rule=%s target=%s",
            log_tag,
            record.id,
            webhook_id,
            event_type,
            rule.name,
            target_type,
        )

    return outbox_ids


def outbox_result(outbox_ids: list[int]) -> ForwardResult:
    if not outbox_ids:
        return {"status": "skipped", "reason": "所有匹配规则均已存在或无效", "outbox_ids": []}
    return {"status": "queued", "outbox_ids": outbox_ids, "outbox_id": outbox_ids[0]}


def idempotency_key(
    *,
    webhook_id: int,
    rule_id: int | None,
    target_type: str,
    target_url: str,
    is_periodic_reminder: bool,
    extra: str = "",
) -> str:
    raw = f"{webhook_id}|{rule_id or 'default'}|{target_type}|{target_url}|{int(is_periodic_reminder)}|{extra}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"forward:{webhook_id}:{digest[:32]}"

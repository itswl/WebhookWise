"""Low-level forwarding outbox record creation helpers."""

from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utcnow
from core.logger import get_logger
from core.observability.metrics import FORWARD_OUTBOX_RECORDS_TOTAL
from models import ForwardOutbox
from services.forwarding.policies import ForwardDeliveryPolicy
from services.forwarding.types import ForwardRuleSnapshot
from services.webhooks.inbound_rules import alert_rule_name, inbound_actions_for
from services.webhooks.types import (
    SKIP_AI,
    SKIP_DEEP_ANALYSIS,
    AnalysisResult,
    ForwardOutboxStatus,
    ForwardResult,
    analysis_route,
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
    # An alert whose rule is excluded from AI analysis must not reach the
    # investigator either. The exclusion cannot be expressed by importance: the
    # rule pass still judges these high — correctly, they are payment alerts —
    # and the deep-analysis rule matches on high, so severity alone would send
    # every one of them to a $0.39 investigation nobody asked for.
    ai_excluded = analysis_route(analysis_result, default="rule") == "rule_excluded"
    if not ai_excluded and any(rule.target_type == "deep_analysis" for rule in matched_rules):
        # Only asked when a deep-analysis target is actually in play: this is a
        # cached lookup, but it is still work in the delivery path.
        #
        # Both actions, not just skip_deep_analysis. The analysis route says
        # `rule_excluded` only when THIS alert went through the exclusion; an
        # identical alert answered from the cache carries `cache` instead, and
        # the cached verdict is `high`, which is exactly what the deep-analysis
        # forward rule matches on. Asking only about skip_deep_analysis let a
        # cache hit fund an investigation the operator had excluded.
        actions = await inbound_actions_for(
            parsed_data=forward_data,
            event_type=event_type,
            importance=str(analysis_result.get("importance") or "") if analysis_result else "",
            rule_name=alert_rule_name(forward_data or {}),
        )
        ai_excluded = bool(actions & {SKIP_AI, SKIP_DEEP_ANALYSIS})

    for rule in matched_rules:
        target_type = str(rule.target_type or "webhook")
        target_url = str(rule.target_url or "")
        if ai_excluded and target_type == "deep_analysis":
            logger.info(
                "[%s] Rule '%s' targets deep analysis, but this alert rule is excluded from AI",
                log_tag,
                rule.name or rule.id,
            )
            FORWARD_OUTBOX_RECORDS_TOTAL.labels(target_type, "skipped_ai_excluded").inc()
            continue
        if target_type != "deep_analysis" and not target_url:
            logger.warning("[%s] Rule '%s' has empty target_url, skipping", log_tag, rule.name or rule.id)
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
        existing = await find_outbox_id_by_key(session, key)
        if existing is not None:
            logger.info("[%s] Idempotency hit key=%s id=%s", log_tag, key, existing)
            FORWARD_OUTBOX_RECORDS_TOTAL.labels(target_type, "duplicate").inc()
            outbox_ids.append(existing)
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
            # Snapshot, not a lookup: a rule edited while this row waits in the
            # queue must not redirect a delivery that was already decided.
            target_gateway=str(getattr(rule, "target_gateway", "") or ""),
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
        outbox_id, created = await insert_outbox_or_existing(session, record)
        outbox_ids.append(outbox_id)
        if not created:
            # Same outcome as the pre-check hit above; only the timing differs.
            logger.info("[%s] Idempotency race lost key=%s id=%s", log_tag, key, outbox_id)
            FORWARD_OUTBOX_RECORDS_TOTAL.labels(target_type, "duplicate").inc()
            continue
        FORWARD_OUTBOX_RECORDS_TOTAL.labels(target_type, "created").inc()
        logger.info(
            "[%s] Created forward intent id=%s event_id=%s event_type=%s rule=%s target=%s",
            log_tag,
            outbox_id,
            webhook_id,
            event_type,
            rule.name,
            target_type,
        )

    return outbox_ids


async def find_outbox_id_by_key(session: AsyncSession, key: str) -> int | None:
    """Id of the committed-and-visible outbox row carrying ``key``, if any."""
    existing = (
        await session.execute(select(ForwardOutbox.id).where(ForwardOutbox.idempotency_key == key))
    ).scalar_one_or_none()
    return int(existing) if existing is not None else None


async def insert_outbox_or_existing(session: AsyncSession, record: ForwardOutbox) -> tuple[int, bool]:
    """Flush ``record`` under a SAVEPOINT; on an idempotency-key collision adopt the winner's row.

    Returns ``(outbox_id, created)``. Two workers can both pass the SELECT
    pre-check for one key before either commits; the UNIQUE index then rejects
    the loser at flush time. Left uncaught, that IntegrityError poisoned the
    caller's whole transaction — on PostgreSQL every later statement fails
    until rollback — so the webhook's persist stage was recorded as failed
    even though the row it wanted already exists. Rolling back to the
    SAVEPOINT keeps the transaction usable, and the re-select returns the row
    the other worker committed: the same outcome as a pre-check hit.

    An IntegrityError that is NOT this collision (a foreign-key or NOT NULL
    violation) has no row to fall back on and is re-raised unchanged.
    """
    try:
        async with session.begin_nested():
            session.add(record)
            await session.flush()
    except IntegrityError:
        existing = await find_outbox_id_by_key(session, record.idempotency_key)
        if existing is None:
            raise
        return existing, False
    return int(record.id), True


def outbox_result(outbox_ids: list[int]) -> ForwardResult:
    if not outbox_ids:
        return {"status": "skipped", "reason": "all matched rules already exist or are invalid", "outbox_ids": []}
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

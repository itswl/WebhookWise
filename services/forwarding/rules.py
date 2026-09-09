"""Forwarding rule CRUD and delivery-health read models."""

import re
from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utc_isoformat, utcnow
from core.logger import mask_url
from core.pubsub_cache import TtlPubSubCache
from db.session import session_scope
from models import ForwardOutbox, ForwardRule
from services.forwarding.types import (
    SYSTEM_EVENT_TYPES,
    ForwardRuleSnapshot,
    matches_only_system_events,
)
from services.webhooks.decision_trace_queries import get_forward_rule_hit_counts

_RULES_INVALIDATION_CHANNEL = "webhookwise:forward_rules:invalidate"
_URL_PATTERN = re.compile(r"https?://[^\s]+", re.IGNORECASE)


def _safe_delivery_error(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return _URL_PATTERN.sub(lambda match: mask_url(match.group(0)), text)[:200]


async def get_forward_rules(session: AsyncSession) -> list[ForwardRule]:
    stmt = select(ForwardRule).order_by(ForwardRule.priority.desc())
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_forward_rule_delivery_health(
    session: AsyncSession,
    rule_ids: list[int],
) -> dict[int, dict[str, Any]]:
    """Return one latest-delivery snapshot plus the recent failure count per rule."""
    if not rule_ids:
        return {}

    ranked = (
        select(
            ForwardOutbox.forward_rule_id.label("rule_id"),
            ForwardOutbox.status,
            ForwardOutbox.last_error,
            ForwardOutbox.updated_at,
            func.row_number()
            .over(
                partition_by=ForwardOutbox.forward_rule_id,
                order_by=(ForwardOutbox.updated_at.desc(), ForwardOutbox.id.desc()),
            )
            .label("row_number"),
        )
        .where(ForwardOutbox.forward_rule_id.in_(rule_ids))
        .subquery()
    )
    latest_rows = (
        await session.execute(
            select(
                ranked.c.rule_id,
                ranked.c.status,
                ranked.c.last_error,
                ranked.c.updated_at,
            ).where(ranked.c.row_number == 1)
        )
    ).all()
    failure_rows = (
        await session.execute(
            select(ForwardOutbox.forward_rule_id, func.count())
            .where(
                ForwardOutbox.forward_rule_id.in_(rule_ids),
                ForwardOutbox.status == "exhausted",
                ForwardOutbox.updated_at >= utcnow() - timedelta(hours=24),
            )
            .group_by(ForwardOutbox.forward_rule_id)
        )
    ).all()
    failure_counts = {int(rule_id): int(count) for rule_id, count in failure_rows if rule_id is not None}
    return {
        int(row.rule_id): {
            "delivery_status": str(row.status or "unknown"),
            "delivery_failure_count_24h": failure_counts.get(int(row.rule_id), 0),
            "last_delivery_at": row.updated_at,
            "last_delivery_error": _safe_delivery_error(row.last_error),
        }
        for row in latest_rows
        if row.rule_id is not None
    }


# The ROI panel's window. Matched to get_forward_rule_hit_counts' 90 days so the
# two halves of one badge cannot disagree about what "recently" means.
_ROI_WINDOW = timedelta(days=90)


async def get_system_event_delivery_counts(
    session: AsyncSession,
    rules: list[ForwardRule],
    *,
    window: timedelta | None = _ROI_WINDOW,
) -> dict[str, dict[str, Any]]:
    """Deliveries per rule that matches ONLY system event types, keyed by name.

    The ROI panel counts a rule's hits from ``decision_trace`` rows, but internal
    system events (incident_created / incident_resolved and friends) are queued
    straight to the outbox by ``resolve_notification_target`` and never write a
    trace. A rule matching only those therefore counted 0 forever and wore the
    "matched nothing" badge while its deliveries were healthy — measured on
    production 2026-09-08: rule 29 had 44 sent outbox rows and zero failures next
    to a zombie badge, two panels contradicting each other over one rule.

    Counts every queued row rather than only ``sent`` ones, because this is the
    analogue of a "forwarded" decision trace: it records that the rule matched
    and produced a delivery. Whether that delivery then succeeded is what the
    delivery-health badge beside it already answers.
    """
    targets = {
        int(rule.id): str(rule.name)
        for rule in rules
        if rule.id is not None and matches_only_system_events(str(getattr(rule, "match_event_type", "") or ""))
    }
    if not targets:
        return {}

    stmt = select(
        ForwardOutbox.forward_rule_id,
        func.count(),
        func.max(ForwardOutbox.created_at),
    ).where(
        ForwardOutbox.forward_rule_id.in_(list(targets)),
        ForwardOutbox.event_type.in_(sorted(SYSTEM_EVENT_TYPES)),
    )
    if window is not None:
        stmt = stmt.where(ForwardOutbox.created_at >= utcnow() - window)
    rows = (await session.execute(stmt.group_by(ForwardOutbox.forward_rule_id))).all()

    return {
        targets[int(rule_id)]: {
            "count": int(count),
            "last_matched_at": utc_isoformat(last_at) if last_at is not None else None,
        }
        for rule_id, count, last_at in rows
        if rule_id is not None and int(rule_id) in targets
    }


async def get_forward_rule_roi(
    session: AsyncSession,
    rules: list[ForwardRule] | None = None,
    *,
    window: timedelta | None = _ROI_WINDOW,
) -> dict[str, dict[str, Any]]:
    """One hit count per rule for the ROI panel, from whichever ledger records it.

    Two ledgers, because two paths reach a target: an alert forwarded by the
    decisioning pipeline writes a ``decision_trace``, and an internal system
    event queued by ``resolve_notification_target`` writes only an outbox row.
    A rule is read from the ledger its traffic actually lands in, and says which
    one in ``hit_count_source`` so the badge can name the right noun — "matched"
    is about alerts and would be a lie about an incident card.

    Every rule passed in gets an entry, zeros included: this is the view that
    answers "which enabled rule has gone quiet", and a rule that has gone quiet
    was previously absent from the answer rather than present with a 0.
    """
    if rules is None:
        rules = await get_forward_rules(session)
    if not rules:
        return {}

    system_only = {
        str(rule.name) for rule in rules if matches_only_system_events(str(getattr(rule, "match_event_type", "") or ""))
    }
    traces = await get_forward_rule_hit_counts(session, rule_names=[str(r.name) for r in rules], window=window)
    deliveries = await get_system_event_delivery_counts(session, rules, window=window)

    roi: dict[str, dict[str, Any]] = {}
    for rule in rules:
        name = str(rule.name)
        source = "system_event_delivery" if name in system_only else "decision_trace"
        stat = (deliveries if name in system_only else traces).get(name)
        roi[name] = {
            "count": stat["count"] if stat else 0,
            "last_matched_at": stat["last_matched_at"] if stat else None,
            "hit_count_source": source,
        }
    return roi


async def create_forward_rule(
    session: AsyncSession,
    name: str,
    target_type: str,
    enabled: bool = True,
    priority: int = 0,
    match_event_type: str = "",
    match_importance: str = "",
    match_duplicate: str = "all",
    match_source: str = "",
    match_project: str = "",
    match_region: str = "",
    match_environment: str = "",
    match_payload: str = "",
    target_url: str = "",
    target_name: str = "",
    target_gateway: str = "",
    stop_on_match: bool = False,
) -> ForwardRule:
    rule = ForwardRule(
        name=name,
        enabled=enabled,
        priority=priority,
        match_event_type=match_event_type,
        match_importance=match_importance,
        match_duplicate=match_duplicate,
        match_source=match_source,
        match_project=match_project,
        match_region=match_region,
        match_environment=match_environment,
        match_payload=match_payload,
        target_type=target_type,
        target_url=target_url,
        target_name=target_name,
        target_gateway=target_gateway,
        stop_on_match=stop_on_match,
    )
    session.add(rule)
    await session.flush()
    _rules_cache.invalidate_after_commit(session)
    return rule


async def get_forward_rule(session: AsyncSession, rule_id: int) -> ForwardRule | None:
    stmt = select(ForwardRule).filter_by(id=rule_id)
    result = await session.execute(stmt)
    return result.scalars().first()


async def update_forward_rule(session: AsyncSession, rule_id: int, payload: Mapping[str, object]) -> ForwardRule | None:
    rule = await get_forward_rule(session, rule_id)
    if not rule:
        return None

    fields = [
        "name",
        "enabled",
        "priority",
        "match_event_type",
        "match_importance",
        "match_duplicate",
        "match_source",
        "match_project",
        "match_region",
        "match_environment",
        "match_payload",
        "target_type",
        "target_url",
        "target_name",
        "target_gateway",
        "stop_on_match",
    ]
    for field in fields:
        if field in payload:
            setattr(rule, field, payload[field])

    rule.updated_at = utcnow()
    await session.flush()
    _rules_cache.invalidate_after_commit(session)
    return rule


async def delete_forward_rule(session: AsyncSession, rule_id: int) -> bool:
    rule = await get_forward_rule(session, rule_id)
    if not rule:
        return False
    await session.delete(rule)
    _rules_cache.invalidate_after_commit(session)
    return True


def _snapshot_forward_rule(rule: ForwardRule) -> ForwardRuleSnapshot:
    return ForwardRuleSnapshot(
        id=rule.id,
        name=rule.name,
        match_event_type=getattr(rule, "match_event_type", "") or "",
        match_importance=rule.match_importance,
        match_source=rule.match_source,
        match_duplicate=rule.match_duplicate,
        match_payload=getattr(rule, "match_payload", "") or "",
        match_project=getattr(rule, "match_project", "") or "",
        match_region=getattr(rule, "match_region", "") or "",
        match_environment=getattr(rule, "match_environment", "") or "",
        target_type=rule.target_type,
        target_url=rule.target_url,
        stop_on_match=rule.stop_on_match,
        target_name=rule.target_name or "",
        target_gateway=getattr(rule, "target_gateway", "") or "",
    )


async def list_enabled_forward_rules(session: AsyncSession | None = None) -> list[ForwardRuleSnapshot]:
    async def _list(sess: AsyncSession) -> list[ForwardRuleSnapshot]:
        stmt = select(ForwardRule).filter_by(enabled=True).order_by(ForwardRule.priority.desc())
        return [_snapshot_forward_rule(rule) for rule in (await sess.execute(stmt)).scalars().all()]

    if session is not None:
        return await _list(session)
    async with session_scope() as sess:
        return await _list(sess)


# Per-worker TTL cache of enabled rules + cross-worker Pub/Sub invalidation.
# Shared helper (see core/pubsub_cache.py); the module-level wrappers below keep
# the existing public names/signatures for call sites and test patches.
_rules_cache: TtlPubSubCache[list[ForwardRuleSnapshot]] = TtlPubSubCache(
    channel=_RULES_INVALIDATION_CHANNEL,
    loader=list_enabled_forward_rules,
    log_prefix="ForwardRules",
)


def invalidate_forward_rules_cache() -> None:
    _rules_cache.invalidate()


async def publish_rules_invalidation() -> None:
    """Broadcast cache invalidation to all workers via Redis Pub/Sub."""
    await _rules_cache.publish_invalidation()


async def get_cached_forward_rules(session: AsyncSession | None = None) -> list[ForwardRuleSnapshot]:
    return await _rules_cache.get(session)


async def start_rules_invalidation_listener() -> None:
    """Subscribe to Redis Pub/Sub for cross-worker cache invalidation.

    Call this once per worker process at startup (e.g. in lifespan). Runs as a
    background task that invalidates the local cache when another worker
    publishes an update.
    """
    _rules_cache.start_listener()


async def stop_rules_invalidation_listener() -> None:
    """Stop the cross-worker invalidation listener during process shutdown."""
    await _rules_cache.stop_listener()

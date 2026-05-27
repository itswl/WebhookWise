"""Forwarding rule CRUD."""

import time
from collections.abc import Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utcnow
from db.session import session_scope
from models import ForwardRule
from services.webhooks.decisioning import ForwardRuleSnapshot


async def get_forward_rules(session: AsyncSession) -> list[ForwardRule]:
    stmt = select(ForwardRule).order_by(ForwardRule.priority.desc())
    result = await session.execute(stmt)
    return list(result.scalars().all())


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
    match_payload: str = "",
    target_url: str = "",
    target_name: str = "",
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
        match_payload=match_payload,
        target_type=target_type,
        target_url=target_url,
        target_name=target_name,
        stop_on_match=stop_on_match,
    )
    session.add(rule)
    await session.flush()
    invalidate_forward_rules_cache()
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
        "match_payload",
        "target_type",
        "target_url",
        "target_name",
        "stop_on_match",
    ]
    for field in fields:
        if field in payload:
            setattr(rule, field, payload[field])

    rule.updated_at = utcnow()
    await session.flush()
    invalidate_forward_rules_cache()
    return rule


async def delete_forward_rule(session: AsyncSession, rule_id: int) -> bool:
    rule = await get_forward_rule(session, rule_id)
    if not rule:
        return False
    await session.delete(rule)
    invalidate_forward_rules_cache()
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
        target_type=rule.target_type,
        target_url=rule.target_url,
        stop_on_match=rule.stop_on_match,
        target_name=rule.target_name or "",
    )


async def list_enabled_forward_rules(session: AsyncSession | None = None) -> list[ForwardRuleSnapshot]:
    async def _list(sess: AsyncSession) -> list[ForwardRuleSnapshot]:
        stmt = select(ForwardRule).filter_by(enabled=True).order_by(ForwardRule.priority.desc())
        return [_snapshot_forward_rule(rule) for rule in (await sess.execute(stmt)).scalars().all()]

    if session is not None:
        return await _list(session)
    async with session_scope() as sess:
        return await _list(sess)


_rules_cache: list[ForwardRuleSnapshot] | None = None
_rules_cache_at: float = 0.0
_RULES_CACHE_TTL: float = 30.0


def invalidate_forward_rules_cache() -> None:
    global _rules_cache, _rules_cache_at
    _rules_cache = None
    _rules_cache_at = 0.0


async def get_cached_forward_rules(session: AsyncSession | None = None) -> list[ForwardRuleSnapshot]:
    global _rules_cache, _rules_cache_at
    now = time.monotonic()
    if _rules_cache is not None and (now - _rules_cache_at) < _RULES_CACHE_TTL:
        return _rules_cache
    rules = await list_enabled_forward_rules(session=session)
    _rules_cache = rules
    _rules_cache_at = now
    return rules

"""Forwarding rule CRUD."""

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import ForwardRule
from services.webhooks.repository import invalidate_forward_rules_cache


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


async def update_forward_rule(session: AsyncSession, rule_id: int, payload: dict[str, Any]) -> ForwardRule | None:
    rule = await get_forward_rule(session, rule_id)
    if not rule:
        return None

    fields = [
        "name",
        "enabled",
        "priority",
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

    rule.updated_at = datetime.now()
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

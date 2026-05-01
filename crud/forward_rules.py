from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import ForwardRule


async def get_forward_rules(session: AsyncSession):
    stmt = select(ForwardRule).order_by(ForwardRule.priority.desc())
    result = await session.execute(stmt)
    return result.scalars().all()


async def create_forward_rule(
    session: AsyncSession,
    name: str,
    target_type: str,
    enabled: bool = True,
    priority: int = 0,
    match_importance: str = "",
    match_duplicate: str = "all",
    match_source: str = "",
    target_url: str = "",
    target_name: str = "",
    stop_on_match: bool = False,
):
    rule = ForwardRule(
        name=name,
        enabled=enabled,
        priority=priority,
        match_importance=match_importance,
        match_duplicate=match_duplicate,
        match_source=match_source,
        target_type=target_type,
        target_url=target_url,
        target_name=target_name,
        stop_on_match=stop_on_match,
    )
    session.add(rule)
    await session.flush()
    return rule


async def get_forward_rule(session: AsyncSession, rule_id: int):
    stmt = select(ForwardRule).filter_by(id=rule_id)
    result = await session.execute(stmt)
    return result.scalars().first()


async def update_forward_rule(session: AsyncSession, rule_id: int, payload: dict):
    rule = await get_forward_rule(session, rule_id)
    if not rule:
        return None

    for field in [
        "name",
        "enabled",
        "priority",
        "match_importance",
        "match_duplicate",
        "match_source",
        "target_type",
        "target_url",
        "target_name",
        "stop_on_match",
    ]:
        if field in payload:
            setattr(rule, field, payload[field])

    rule.updated_at = datetime.now()
    await session.flush()
    return rule


async def delete_forward_rule(session: AsyncSession, rule_id: int):
    rule = await get_forward_rule(session, rule_id)
    if not rule:
        return False
    session.delete(rule)
    return True

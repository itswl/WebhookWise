"""Forwarding rule CRUD."""

import contextlib
import time
from collections.abc import Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utcnow
from core.logger import get_logger
from db.session import session_scope
from models import ForwardRule
from services.webhooks.decisioning import ForwardRuleSnapshot

logger = get_logger("forwarding.rules")

_RULES_INVALIDATION_CHANNEL = "webhookwise:forward_rules:invalidate"


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
    match_project: str = "",
    match_region: str = "",
    match_environment: str = "",
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
        match_project=match_project,
        match_region=match_region,
        match_environment=match_environment,
        match_payload=match_payload,
        target_type=target_type,
        target_url=target_url,
        target_name=target_name,
        stop_on_match=stop_on_match,
    )
    session.add(rule)
    await session.flush()
    invalidate_forward_rules_cache()
    await publish_rules_invalidation()
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
        "stop_on_match",
    ]
    for field in fields:
        if field in payload:
            setattr(rule, field, payload[field])

    rule.updated_at = utcnow()
    await session.flush()
    invalidate_forward_rules_cache()
    await publish_rules_invalidation()
    return rule


async def delete_forward_rule(session: AsyncSession, rule_id: int) -> bool:
    rule = await get_forward_rule(session, rule_id)
    if not rule:
        return False
    await session.delete(rule)
    invalidate_forward_rules_cache()
    await publish_rules_invalidation()
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


async def publish_rules_invalidation() -> None:
    """Broadcast cache invalidation to all workers via Redis Pub/Sub."""
    try:
        from core.redis_client import redis_publish

        await redis_publish(_RULES_INVALIDATION_CHANNEL, "invalidate")
    except Exception as e:
        logger.warning("[ForwardRules] 发布缓存失效通知失败: %s", e)


async def get_cached_forward_rules(session: AsyncSession | None = None) -> list[ForwardRuleSnapshot]:
    global _rules_cache, _rules_cache_at
    now = time.monotonic()
    if _rules_cache is not None and (now - _rules_cache_at) < _RULES_CACHE_TTL:
        return _rules_cache
    rules = await list_enabled_forward_rules(session=session)
    _rules_cache = rules
    _rules_cache_at = now
    return rules


async def start_rules_invalidation_listener() -> None:
    """Subscribe to Redis Pub/Sub for cross-worker cache invalidation.

    Call this once per worker process at startup (e.g. in lifespan).
    Runs as a background task that invalidates the local cache when
    another worker publishes an update.
    """
    import asyncio

    from redis.exceptions import RedisError

    from core.redis_client import get_redis

    async def _listen() -> None:
        client = get_redis()
        pubsub = client.pubsub()
        try:
            await pubsub.subscribe(_RULES_INVALIDATION_CHANNEL)
            async for message in pubsub.listen():
                if message.get("type") == "message":
                    invalidate_forward_rules_cache()
                    logger.debug("[ForwardRules] 收到跨进程缓存失效通知")
        except (RedisError, OSError, RuntimeError) as e:
            logger.warning("[ForwardRules] Pub/Sub 监听中断: %s", e)
        finally:
            with contextlib.suppress(Exception):
                await pubsub.unsubscribe(_RULES_INVALIDATION_CHANNEL)
                await pubsub.aclose()

    asyncio.create_task(_listen())

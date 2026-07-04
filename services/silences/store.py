"""Silence (manual mute / snooze) CRUD + active-silence cache.

A silence is the deny counterpart to a forwarding rule: while active it stops
matching alerts from being forwarded. The active set is read on every forward
decision, so it is cached per worker (30s TTL) with cross-worker invalidation
over Redis Pub/Sub — the exact pattern used by services/forwarding/rules.py.
"""

from collections.abc import Mapping

from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utcnow
from core.pubsub_cache import TtlPubSubCache
from db.session import session_scope
from models import Silence
from services.webhooks.decisioning import SilenceSnapshot

_SILENCES_INVALIDATION_CHANNEL = "webhookwise:silences:invalidate"

_MATCH_FIELDS = (
    "match_source",
    "match_importance",
    "match_event_type",
    "match_project",
    "match_region",
    "match_environment",
    "match_payload",
)


def _active_filter() -> ColumnElement[bool]:
    """SQL predicate for "currently active": not lifted, not expired."""
    now = utcnow()
    return Silence.lifted_at.is_(None) & or_(Silence.expires_at.is_(None), Silence.expires_at > now)


async def list_silences(session: AsyncSession, *, active_only: bool = False) -> list[Silence]:
    stmt = select(Silence).order_by(Silence.created_at.desc())
    if active_only:
        stmt = stmt.where(_active_filter())
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_silence(session: AsyncSession, silence_id: int) -> Silence | None:
    result = await session.execute(select(Silence).filter_by(id=silence_id))
    return result.scalars().first()


async def create_silence(
    session: AsyncSession,
    *,
    match_source: str = "",
    match_importance: str = "",
    match_event_type: str = "",
    match_project: str = "",
    match_region: str = "",
    match_environment: str = "",
    match_payload: str = "",
    comment: str = "",
    created_by: str = "",
    expires_at: object | None = None,
) -> Silence:
    silence = Silence(
        match_source=match_source,
        match_importance=match_importance,
        match_event_type=match_event_type,
        match_project=match_project,
        match_region=match_region,
        match_environment=match_environment,
        match_payload=match_payload,
        comment=comment,
        created_by=created_by,
        expires_at=expires_at,
    )
    session.add(silence)
    await session.flush()
    invalidate_silences_cache()
    await publish_silences_invalidation()
    return silence


async def update_silence(session: AsyncSession, silence_id: int, payload: Mapping[str, object]) -> Silence | None:
    silence = await get_silence(session, silence_id)
    if not silence:
        return None
    fields = (*_MATCH_FIELDS, "comment", "expires_at")
    for field in fields:
        if field in payload:
            setattr(silence, field, payload[field])
    await session.flush()
    invalidate_silences_cache()
    await publish_silences_invalidation()
    return silence


async def lift_silence(session: AsyncSession, silence_id: int) -> Silence | None:
    """Soft-lift a silence: mark it inactive without deleting the audit row."""
    silence = await get_silence(session, silence_id)
    if not silence:
        return None
    if silence.lifted_at is None:
        silence.lifted_at = utcnow()
    await session.flush()
    invalidate_silences_cache()
    await publish_silences_invalidation()
    return silence


async def delete_silence(session: AsyncSession, silence_id: int) -> bool:
    silence = await get_silence(session, silence_id)
    if not silence:
        return False
    await session.delete(silence)
    invalidate_silences_cache()
    await publish_silences_invalidation()
    return True


def _snapshot_silence(silence: Silence) -> SilenceSnapshot:
    return SilenceSnapshot(
        id=silence.id,
        match_event_type=silence.match_event_type or "",
        match_importance=silence.match_importance or "",
        match_source=silence.match_source or "",
        match_payload=silence.match_payload or "",
        match_project=silence.match_project or "",
        match_region=silence.match_region or "",
        match_environment=silence.match_environment or "",
        comment=silence.comment or "",
    )


async def list_active_silences(session: AsyncSession | None = None) -> list[SilenceSnapshot]:
    async def _list(sess: AsyncSession) -> list[SilenceSnapshot]:
        stmt = select(Silence).where(_active_filter())
        return [_snapshot_silence(s) for s in (await sess.execute(stmt)).scalars().all()]

    if session is not None:
        return await _list(session)
    async with session_scope() as sess:
        return await _list(sess)


# Per-worker TTL cache of active silences + cross-worker Pub/Sub invalidation.
# Shared helper (see core/pubsub_cache.py); the module-level wrappers below keep
# the existing public names/signatures for call sites and test patches.
_silences_cache: TtlPubSubCache[list[SilenceSnapshot]] = TtlPubSubCache(
    channel=_SILENCES_INVALIDATION_CHANNEL,
    loader=list_active_silences,
    log_prefix="Silences",
)


def invalidate_silences_cache() -> None:
    _silences_cache.invalidate()


async def publish_silences_invalidation() -> None:
    """Broadcast cache invalidation to all workers via Redis Pub/Sub."""
    await _silences_cache.publish_invalidation()


async def get_cached_active_silences(session: AsyncSession | None = None) -> list[SilenceSnapshot]:
    return await _silences_cache.get(session)


async def start_silences_invalidation_listener() -> None:
    """Subscribe to Redis Pub/Sub for cross-worker cache invalidation.

    Call this once per worker process at startup (e.g. in lifespan). Runs as a
    background task that invalidates the local cache when another worker
    publishes an update.
    """
    _silences_cache.start_listener()

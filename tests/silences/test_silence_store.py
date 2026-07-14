"""Silence store CRUD, active-filter, and snapshot tests (in-memory sqlite)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.datetime_utils import utcnow
from services.silences import store


@pytest.fixture
async def session(db_session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    # These tests rely on the auto-committing transaction wrapper (factory.begin())
    # rather than the plain shared `session` fixture, so wrap the shared factory.
    async with db_session_factory.begin() as sess:
        yield sess


@pytest.fixture(autouse=True)
def _reset_cache():
    store.invalidate_silences_cache()
    yield
    store.invalidate_silences_cache()


@pytest.mark.asyncio
async def test_create_and_list(session: AsyncSession) -> None:
    created = await store.create_silence(session, match_source="prometheus", comment="maintenance")
    assert created.id is not None
    rows = await store.list_silences(session)
    assert [r.match_source for r in rows] == ["prometheus"]
    assert rows[0].lifted_at is None


@pytest.mark.asyncio
async def test_active_filter_excludes_expired(session: AsyncSession) -> None:
    await store.create_silence(session, match_source="active", expires_at=utcnow() + timedelta(hours=1))
    await store.create_silence(session, match_source="expired", expires_at=utcnow() - timedelta(hours=1))
    await store.create_silence(session, match_source="permanent", expires_at=None)
    active = await store.list_active_silences(session)
    sources = {s.match_source for s in active}
    assert sources == {"active", "permanent"}


@pytest.mark.asyncio
async def test_lift_makes_inactive(session: AsyncSession) -> None:
    created = await store.create_silence(session, match_source="prometheus")
    assert len(await store.list_active_silences(session)) == 1
    lifted = await store.lift_silence(session, created.id)
    assert lifted is not None
    assert lifted.lifted_at is not None
    assert await store.list_active_silences(session) == []
    # Still present in the full list (soft-lift keeps the audit row).
    assert len(await store.list_silences(session)) == 1


@pytest.mark.asyncio
async def test_lift_is_idempotent(session: AsyncSession) -> None:
    created = await store.create_silence(session, match_source="prometheus")
    first = await store.lift_silence(session, created.id)
    assert first is not None
    first_lifted_at = first.lifted_at
    second = await store.lift_silence(session, created.id)
    assert second is not None
    assert second.lifted_at == first_lifted_at


@pytest.mark.asyncio
async def test_update_fields(session: AsyncSession) -> None:
    created = await store.create_silence(session, match_source="prometheus", comment="old")
    updated = await store.update_silence(session, created.id, {"comment": "new", "match_importance": "high"})
    assert updated is not None
    assert updated.comment == "new"
    assert updated.match_importance == "high"


@pytest.mark.asyncio
async def test_delete(session: AsyncSession) -> None:
    created = await store.create_silence(session, match_source="prometheus")
    assert await store.delete_silence(session, created.id) is True
    assert await store.list_silences(session) == []
    assert await store.delete_silence(session, created.id) is False


@pytest.mark.asyncio
async def test_snapshot_shape(session: AsyncSession) -> None:
    await store.create_silence(
        session,
        match_source="prometheus",
        match_importance="high",
        match_event_type="alert",
        match_project="eve-cn",
        match_region="cn-north",
        match_environment="prod",
        match_payload="labels.team=infra",
        comment="snap",
    )
    [snap] = await store.list_active_silences(session)
    assert snap.match_source == "prometheus"
    assert snap.match_importance == "high"
    assert snap.match_event_type == "alert"
    assert snap.match_project == "eve-cn"
    assert snap.match_region == "cn-north"
    assert snap.match_environment == "prod"
    assert snap.match_payload == "labels.team=infra"
    assert snap.comment == "snap"


@pytest.mark.asyncio
async def test_get_missing_returns_none(session: AsyncSession) -> None:
    assert await store.get_silence(session, 9999) is None
    assert await store.update_silence(session, 9999, {"comment": "x"}) is None
    assert await store.lift_silence(session, 9999) is None

"""Alert acknowledgement service tests (in-memory sqlite)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from models import WebhookEvent
from services.webhooks.acknowledgement import acknowledge_webhook, unacknowledge_webhook


@pytest.fixture()
async def session() -> AsyncIterator[AsyncSession]:
    import models  # noqa: F401
    from db.session import Base

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory.begin() as sess:
        yield sess
    await engine.dispose()


async def _make_event(session: AsyncSession, *, duplicate_of: int | None = None) -> WebhookEvent:
    event = WebhookEvent(source="prometheus", is_duplicate=duplicate_of is not None, duplicate_of=duplicate_of)
    session.add(event)
    await session.flush()
    return event


@pytest.mark.asyncio
async def test_acknowledge_sets_columns(session: AsyncSession) -> None:
    event = await _make_event(session)
    head = await acknowledge_webhook(session, event.id, acknowledged_by="alice")
    assert head is not None
    assert head.id == event.id
    assert head.acknowledged_at is not None
    assert head.acknowledged_by == "alice"


@pytest.mark.asyncio
async def test_acknowledge_is_first_ack_wins(session: AsyncSession) -> None:
    event = await _make_event(session)
    first = await acknowledge_webhook(session, event.id, acknowledged_by="alice")
    assert first is not None
    first_at = first.acknowledged_at
    second = await acknowledge_webhook(session, event.id, acknowledged_by="bob")
    assert second is not None
    # Idempotent: neither the timestamp nor the original acker changes.
    assert second.acknowledged_at == first_at
    assert second.acknowledged_by == "alice"


@pytest.mark.asyncio
async def test_acknowledge_duplicate_applies_to_chain_head(session: AsyncSession) -> None:
    head = await _make_event(session)
    dup = await _make_event(session, duplicate_of=head.id)
    # Acking the duplicate occurrence acks the chain head.
    result = await acknowledge_webhook(session, dup.id, acknowledged_by="alice")
    assert result is not None
    assert result.id == head.id
    refreshed_head = await session.get(WebhookEvent, head.id)
    refreshed_dup = await session.get(WebhookEvent, dup.id)
    assert refreshed_head is not None and refreshed_head.acknowledged_at is not None
    assert refreshed_dup is not None and refreshed_dup.acknowledged_at is None


@pytest.mark.asyncio
async def test_unacknowledge_clears(session: AsyncSession) -> None:
    event = await _make_event(session)
    await acknowledge_webhook(session, event.id, acknowledged_by="alice")
    cleared = await unacknowledge_webhook(session, event.id)
    assert cleared is not None
    assert cleared.acknowledged_at is None
    assert cleared.acknowledged_by is None


@pytest.mark.asyncio
async def test_acknowledge_missing_returns_none(session: AsyncSession) -> None:
    assert await acknowledge_webhook(session, 9999) is None
    assert await unacknowledge_webhook(session, 9999) is None

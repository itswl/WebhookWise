"""
Tests for paginated query functionality.
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from db.session import Base
from models import WebhookEvent
from services.webhooks.query_service import (
    count_webhook_summaries,
    list_webhook_summaries,
    window_to_time_from,
)


@pytest.fixture()
async def mock_session_scope() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    Session = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    # Insert some test data
    async with Session() as session:
        for _i in range(1, 16):
            event = WebhookEvent(
                source="test",
                importance="high",
                is_duplicate=False,
                duplicate_count=1,
            )
            session.add(event)
        await session.commit()

    yield Session
    await engine.dispose()


@pytest.mark.asyncio
async def test_list_webhook_summaries_pagination(
    mock_session_scope: async_sessionmaker[AsyncSession],
) -> None:
    # Test the first page
    async with mock_session_scope() as session:
        webhooks, has_more, next_cursor = await list_webhook_summaries(session, cursor=None, page_size=5)
    assert len(webhooks) == 5
    assert has_more is True
    assert next_cursor == 11

    # Test the second page
    async with mock_session_scope() as session:
        webhooks, has_more, next_cursor = await list_webhook_summaries(session, cursor=11, page_size=5)
    assert len(webhooks) == 5
    assert has_more is True
    assert next_cursor == 6

    # Test the third page
    async with mock_session_scope() as session:
        webhooks, has_more, next_cursor = await list_webhook_summaries(session, cursor=6, page_size=5)
    assert len(webhooks) == 5
    assert has_more is False
    assert next_cursor is None  # 5, 4, 3, 2, 1 (no more left)

    # Verify the last page
    async with mock_session_scope() as session:
        webhooks, has_more, next_cursor = await list_webhook_summaries(session, cursor=1, page_size=5)
    assert len(webhooks) == 0
    assert has_more is False
    assert next_cursor is None


@pytest.mark.asyncio
async def test_list_webhook_summaries_page_offset_without_cursor(
    mock_session_scope: async_sessionmaker[AsyncSession],
) -> None:
    async with mock_session_scope() as session:
        webhooks, has_more, next_cursor = await list_webhook_summaries(session, page=2, page_size=5)

    assert [item["id"] for item in webhooks] == [10, 9, 8, 7, 6]
    assert has_more is True
    assert next_cursor == 6


@pytest.fixture()
async def windowed_session() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    from datetime import timedelta

    from sqlalchemy.ext.asyncio import create_async_engine

    from core.datetime_utils import utcnow

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    now = utcnow()
    async with Session() as session:
        # 3 recent (within 24h) + 2 old (40 days ago).
        for _ in range(3):
            session.add(WebhookEvent(source="test", importance="high", is_duplicate=False, timestamp=now))
        for _ in range(2):
            session.add(
                WebhookEvent(source="test", importance="low", is_duplicate=False, timestamp=now - timedelta(days=40))
            )
        await session.commit()
    yield Session
    await engine.dispose()


def test_window_to_time_from_presets() -> None:
    assert window_to_time_from("all") is None
    assert window_to_time_from("") is None
    assert window_to_time_from("today") is not None
    assert window_to_time_from("7d") is not None


@pytest.mark.asyncio
async def test_list_and_count_respect_time_window(
    windowed_session: async_sessionmaker[AsyncSession],
) -> None:
    from datetime import timedelta

    from core.datetime_utils import utcnow

    cutoff = utcnow() - timedelta(days=1)
    async with windowed_session() as session:
        all_items, _, _ = await list_webhook_summaries(session, page_size=50)
        recent_items, _, _ = await list_webhook_summaries(session, time_from=cutoff, page_size=50)
        total_all = await count_webhook_summaries(session)
        total_recent = await count_webhook_summaries(session, time_from=cutoff)

    assert len(all_items) == 5
    assert len(recent_items) == 3  # the 40-day-old ones are excluded
    assert total_all == 5
    assert total_recent == 3

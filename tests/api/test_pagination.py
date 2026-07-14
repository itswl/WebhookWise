"""
Tests for paginated query functionality.
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from models import WebhookEvent
from services.webhooks.query_service import (
    count_webhook_summaries,
    list_webhook_summaries,
    window_to_time_from,
)


@pytest.fixture
async def mock_session_scope(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    # Seed the shared in-memory database, then hand back the factory so the test
    # can open its own sessions (StaticPool shares one connection, so committed
    # rows are visible to them).
    async with db_session_factory() as session:
        for _i in range(1, 16):
            event = WebhookEvent(
                source="test",
                importance="high",
                is_duplicate=False,
                duplicate_count=1,
            )
            session.add(event)
        await session.commit()

    yield db_session_factory


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


@pytest.fixture
async def windowed_session(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    from datetime import timedelta

    from core.datetime_utils import utcnow

    now = utcnow()
    async with db_session_factory() as session:
        # 3 recent (within 24h) + 2 old (40 days ago).
        for _ in range(3):
            session.add(WebhookEvent(source="test", importance="high", is_duplicate=False, timestamp=now))
        for _ in range(2):
            session.add(
                WebhookEvent(source="test", importance="low", is_duplicate=False, timestamp=now - timedelta(days=40))
            )
        await session.commit()
    yield db_session_factory


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


@pytest.mark.asyncio
async def test_unfiltered_count_uses_planner_estimate_on_postgresql() -> None:
    """The default dashboard open must not pay a full-table COUNT(*) on PG."""
    from services.webhooks.query_service import count_webhook_summaries

    executed: list[str] = []

    class _Result:
        def scalar(self) -> int:
            return 12345

    class _Dialect:
        name = "postgresql"

    class _Bind:
        dialect = _Dialect()

    class _Session:
        def get_bind(self) -> _Bind:
            return _Bind()

        async def execute(self, stmt: object, params: object | None = None) -> _Result:
            executed.append(str(stmt))
            return _Result()

    total = await count_webhook_summaries(_Session())  # type: ignore[arg-type]

    assert total == 12345
    assert len(executed) == 1
    assert "reltuples" in executed[0]


@pytest.mark.asyncio
async def test_small_table_estimate_falls_through_to_exact_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """Below the threshold the lagging planner estimate must not be shown as the total."""
    from db import session as db_session_module
    from services.webhooks.query_service import count_webhook_summaries

    executed: list[str] = []

    class _EstimateResult:
        def scalar(self) -> int:
            return 500  # below _COUNT_ESTIMATE_MIN_ROWS

    class _Dialect:
        name = "postgresql"

    class _Bind:
        dialect = _Dialect()

    class _Session:
        def get_bind(self) -> _Bind:
            return _Bind()

        async def execute(self, stmt: object, params: object | None = None) -> _EstimateResult:
            executed.append(str(stmt))
            return _EstimateResult()

    async def exact_count(session: object, stmt: object, timeout_ms: int = 2000) -> int:
        executed.append("exact-count")
        return 777

    monkeypatch.setattr(db_session_module, "count_with_timeout", exact_count)

    total = await count_webhook_summaries(_Session())  # type: ignore[arg-type]

    assert total == 777
    assert "reltuples" in executed[0]
    assert executed[-1] == "exact-count"

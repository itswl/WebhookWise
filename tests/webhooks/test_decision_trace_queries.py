from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from models import DecisionTrace
from services.webhooks.decision_trace_queries import (
    get_decision_trace_for_event,
    get_decision_trace_stats,
    list_decision_traces,
)


@pytest.fixture()
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
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
    try:
        yield factory
    finally:
        await engine.dispose()


def _trace(event_id: int, outcome: str, skip_code: str, **extra: Any) -> DecisionTrace:
    return DecisionTrace(
        webhook_event_id=event_id,
        outcome=outcome,
        skip_code=skip_code,
        source=extra.get("source", "volcengine"),
        importance=extra.get("importance", "medium"),
        is_periodic_reminder=extra.get("is_periodic_reminder", False),
        matched_rules=extra.get("matched_rules", []),
        steps=extra.get("steps", [{"step": "forward", "outcome": outcome, "skip_code": skip_code}]),
    )


async def _seed(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory.begin() as session:
        session.add_all(
            [
                _trace(1, "forwarded", "none", matched_rules=["feishu"]),
                _trace(2, "skipped", "silenced"),
                _trace(3, "skipped", "silenced"),
                _trace(4, "skipped", "cooldown"),
            ]
        )


@pytest.mark.asyncio
async def test_stats_aggregates_outcome_and_skip_code(session_factory: async_sessionmaker[AsyncSession]) -> None:
    await _seed(session_factory)
    async with session_factory() as session:
        stats = await get_decision_trace_stats(session, "day")

    assert stats["total"] == 4
    assert stats["forwarded"] == 1
    assert stats["skipped"] == 3
    assert stats["outcome_breakdown"] == {"forwarded": 1, "skipped": 3}
    # Skip distribution is over skipped traces only (forwarded's "none" excluded).
    assert stats["skip_code_breakdown"] == {"silenced": 2, "cooldown": 1}
    assert "none" not in stats["skip_code_breakdown"]


@pytest.mark.asyncio
async def test_list_filters_by_skip_code_and_carries_steps(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(session_factory)
    async with session_factory() as session:
        items, has_more, next_cursor = await list_decision_traces(session, skip_code="silenced")

    assert {item["webhook_event_id"] for item in items} == {2, 3}
    assert all(item["skip_code"] == "silenced" for item in items)
    assert has_more is False
    assert next_cursor is None
    # The full chain ships inline with each row.
    assert items[0]["steps"]


@pytest.mark.asyncio
async def test_list_filters_by_outcome(session_factory: async_sessionmaker[AsyncSession]) -> None:
    await _seed(session_factory)
    async with session_factory() as session:
        items, _, _ = await list_decision_traces(session, outcome="forwarded")
    assert len(items) == 1
    assert items[0]["webhook_event_id"] == 1
    assert items[0]["matched_rules"] == ["feishu"]


@pytest.mark.asyncio
async def test_list_paginates_with_cursor(session_factory: async_sessionmaker[AsyncSession]) -> None:
    await _seed(session_factory)
    async with session_factory() as session:
        first, has_more, next_cursor = await list_decision_traces(session, page_size=2)
        assert has_more is True
        assert next_cursor is not None
        # Newest first: ids 4, 3 on the first page.
        assert [item["webhook_event_id"] for item in first] == [4, 3]

        second, has_more2, _ = await list_decision_traces(session, page_size=2, cursor=next_cursor)
        assert [item["webhook_event_id"] for item in second] == [2, 1]
        assert has_more2 is False


@pytest.mark.asyncio
async def test_get_for_event_returns_latest_or_none(session_factory: async_sessionmaker[AsyncSession]) -> None:
    await _seed(session_factory)
    async with session_factory() as session:
        found = await get_decision_trace_for_event(session, 2)
        assert found is not None
        assert found["webhook_event_id"] == 2
        assert found["outcome"] == "skipped"

        missing = await get_decision_trace_for_event(session, 999)
        assert missing is None

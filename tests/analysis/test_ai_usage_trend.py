"""Tests for the per-day AI usage trend series (cost chart backing)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from core.datetime_utils import utcnow
from db.session import Base
from models import AIUsageLog
from services.analysis.analysis_queries import get_ai_usage_stats


@pytest.fixture()
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_trend_groups_by_day_with_route_and_cost(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = utcnow()
    yesterday = now - timedelta(days=1)
    async with session_factory.begin() as session:
        session.add_all(
            [
                # Today: 1 AI call ($0.01) + 1 cache reuse.
                AIUsageLog(timestamp=now, route_type="ai", tokens_in=100, tokens_out=50, cost_estimate=0.01),
                AIUsageLog(timestamp=now, route_type="cache", tokens_in=0, tokens_out=0, cost_estimate=0.0),
                # Yesterday: 1 rule call.
                AIUsageLog(timestamp=yesterday, route_type="rule", tokens_in=0, tokens_out=0, cost_estimate=0.0),
            ]
        )

    async with session_factory() as session:
        stats = await get_ai_usage_stats(session, "week")

    trend = stats["trend"]
    assert isinstance(trend, list) and len(trend) == 2  # two distinct days
    by_day = {p["time"]: p for p in trend}
    today_key = str(now.date())
    yest_key = str(yesterday.date())
    assert by_day[today_key]["total_calls"] == 2
    assert by_day[today_key]["ai_calls"] == 1
    assert by_day[today_key]["rule_calls"] == 0
    assert by_day[today_key]["cost"] == pytest.approx(0.01)
    assert by_day[today_key]["tokens"] == 150
    assert by_day[yest_key]["rule_calls"] == 1
    assert by_day[yest_key]["ai_calls"] == 0


@pytest.mark.asyncio
async def test_trend_empty_when_no_usage(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as session:
        stats = await get_ai_usage_stats(session, "day")
    assert stats["trend"] == []

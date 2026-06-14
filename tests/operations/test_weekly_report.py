"""Tests for the alert-health weekly report (#A)."""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from core.datetime_utils import utcnow
from db.session import Base
from models import AIUsageLog, WebhookEvent


@pytest.fixture()
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as s:
        yield s
    await engine.dispose()


@pytest.mark.asyncio
async def test_collect_report_stats_aggregates_noise_sources_and_cost(session: AsyncSession) -> None:
    from services.operations.weekly_report import collect_report_stats

    now = utcnow()
    # 10 events: 3 duplicates; sources prometheus x6, grafana x4; mixed importance.
    for i in range(10):
        session.add(
            WebhookEvent(
                source="prometheus" if i < 6 else "grafana",
                importance="high" if i < 2 else "low",
                is_duplicate=i < 3,
                timestamp=now,
                duplicate_count=1,
            )
        )
    # AI usage: 2 calls, one a cache hit, total cost 0.05.
    session.add(AIUsageLog(timestamp=now, model="m", cost_estimate=0.03, cache_hit=False))
    session.add(AIUsageLog(timestamp=now, model="m", cost_estimate=0.02, cache_hit=True))
    await session.commit()

    stats = await collect_report_stats(session, window_days=7)

    assert stats["total_events"] == 10
    assert stats["duplicate_events"] == 3
    assert stats["noise_pct"] == 30.0
    assert stats["top_sources"][0] == {"source": "prometheus", "count": 6}
    assert stats["importance_breakdown"] == {"high": 2, "low": 8}
    assert stats["ai_calls"] == 2
    assert stats["ai_cost_usd"] == 0.05
    assert stats["cache_hit_pct"] == 50.0


@pytest.mark.asyncio
async def test_collect_report_stats_excludes_events_outside_window(session: AsyncSession) -> None:
    from datetime import timedelta

    from services.operations.weekly_report import collect_report_stats

    now = utcnow()
    session.add(WebhookEvent(source="s", timestamp=now, duplicate_count=1))
    session.add(WebhookEvent(source="s", timestamp=now - timedelta(days=30), duplicate_count=1))
    await session.commit()

    stats = await collect_report_stats(session, window_days=7)
    assert stats["total_events"] == 1  # the 30-day-old one is excluded


@pytest.mark.asyncio
async def test_weekly_report_no_op_when_disabled(temp_config) -> None:
    from services.operations.weekly_report import generate_and_send_weekly_report

    temp_config.notifications.WEEKLY_REPORT_ENABLED = False
    result = await generate_and_send_weekly_report()
    assert result == {"skipped": "disabled"}


@pytest.mark.asyncio
async def test_weekly_report_skips_when_no_webhook(temp_config) -> None:
    from services.operations.weekly_report import generate_and_send_weekly_report

    temp_config.notifications.WEEKLY_REPORT_ENABLED = True
    temp_config.notifications.WEEKLY_REPORT_FEISHU_WEBHOOK = ""
    temp_config.notifications.DEEP_ANALYSIS_FEISHU_WEBHOOK = ""
    result = await generate_and_send_weekly_report()
    assert result == {"skipped": "no_webhook"}


def test_build_summary_is_deterministic_and_human_readable() -> None:
    from services.operations.weekly_report import _build_summary

    stats = {
        "window_days": 7,
        "total_events": 100,
        "duplicate_events": 40,
        "noise_pct": 40.0,
        "importance_breakdown": {"high": 10, "low": 90},
        "top_sources": [{"source": "prometheus", "count": 55}],
        "ai_cost_usd": 1.23,
        "ai_calls": 60,
        "cache_hit_pct": 25.0,
    }
    text = _build_summary(stats)
    assert "100" in text and "40.0%" in text and "prometheus" in text and "$1.23" in text

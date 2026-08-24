"""Tests for the per-day AI usage trend series (cost chart backing)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.datetime_utils import utcnow
from models import AIUsageLog
from services.analysis.analysis_queries import get_ai_usage_stats


@pytest.fixture
def session_factory(db_session_factory):
    return db_session_factory


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


@pytest.mark.asyncio
async def test_cost_figures_declare_the_rates_they_were_computed_from(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A currency total nobody can trace is the one that gets believed.

    Measured on production 2026-08-21: $16.50 lifetime, computed at the shipped
    Claude-era rates ($0.003/$0.015 per 1k) while the configured model was
    deepseek-v4-pro. The number was confidently wrong and nothing on the screen
    said so. Rather than substitute another guess at a provider's price list,
    the payload now carries what it used.
    """
    from core.app_context import get_config_manager
    from services.analysis.analysis_queries import get_ai_usage_stats

    ai = get_config_manager().ai
    ai.AI_COST_PER_1K_INPUT_TOKENS = 0.003
    ai.AI_COST_PER_1K_OUTPUT_TOKENS = 0.015

    async with session_factory() as session:
        basis = (await get_ai_usage_stats(session, period="week"))["cost"]["basis"]

    assert basis["input_per_1k_usd"] == 0.003
    assert basis["output_per_1k_usd"] == 0.015
    # Untouched defaults are the case worth flagging: it means nobody has ever
    # checked these against the provider that is actually being billed.
    assert basis["rates_are_defaults"] is True
    assert basis["reconciled_with_provider"] is False

    ai.AI_COST_PER_1K_INPUT_TOKENS = 0.00027
    ai.AI_COST_PER_1K_OUTPUT_TOKENS = 0.0011
    async with session_factory() as session:
        basis = (await get_ai_usage_stats(session, period="week"))["cost"]["basis"]
    assert basis["rates_are_defaults"] is False
    assert basis["reconciled_with_provider"] is True


def test_the_response_model_does_not_drop_the_cost_basis() -> None:
    """The service adding a field is not enough: FastAPI filters the payload
    through the response model, so an undeclared field reaches the client as
    null. That is how the first version of this shipped — verified against
    production, `basis` was null while the service was computing it."""
    from schemas.ai_usage import AIUsageData

    payload = {
        "total_calls": 1,
        "route_breakdown": {"ai": 1},
        "percentages": {"ai": 100.0},
        "tokens": {"total": 3, "input": 2, "output": 1},
        "cost": {
            "total": 0.01,
            "saved_estimate": 0.0,
            "basis": {
                "model": "deepseek-v4-pro",
                "input_per_1k_usd": 0.003,
                "output_per_1k_usd": 0.015,
                "rates_are_defaults": True,
                "reconciled_with_provider": False,
            },
        },
        "cache_statistics": {
            "total_cache_entries": 0,
            "total_hits": 0,
            "avg_hits_per_entry": 0.0,
            "cache_hit_rate": 0.0,
            "saved_calls": 0,
        },
        "trend": [],
    }

    serialized = AIUsageData.model_validate(payload).model_dump()

    assert serialized["cost"]["basis"]["model"] == "deepseek-v4-pro"
    assert serialized["cost"]["basis"]["reconciled_with_provider"] is False

"""Tests for cross-source spike detection (activity view backing)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.datetime_utils import utcnow
from models import WebhookEvent
from services.analysis.cross_source import find_cross_source_spikes


@pytest.fixture
def session_factory(db_session_factory):
    return db_session_factory


def _event(source: str, timestamp: object) -> WebhookEvent:
    event = WebhookEvent()
    event.source = source
    event.timestamp = timestamp  # type: ignore[assignment]
    event.parsed_data = {}
    return event


@pytest.mark.asyncio
async def test_detects_multi_source_hours_and_skips_single_source_hours(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Anchor mid-hour so +/- a few minutes never crosses an hour boundary.
    anchor = utcnow().replace(minute=30, second=0, microsecond=0) - timedelta(hours=2)
    lone_hour = anchor - timedelta(hours=3)
    async with session_factory.begin() as session:
        session.add_all(
            [
                # Two sources firing in the same hour → a cross-source bucket.
                _event("prometheus", anchor),
                _event("prometheus", anchor + timedelta(minutes=5)),
                _event("volcengine", anchor + timedelta(minutes=10)),
                # A single-source hour → must not be reported.
                _event("grafana", lone_hour),
            ]
        )

    async with session_factory() as session:
        spikes = await find_cross_source_spikes(session)

    assert len(spikes) == 1
    bucket = spikes[0]
    assert bucket["source_count"] == 2
    counts = {item["source"]: item["count"] for item in bucket["sources"]}
    assert counts == {"prometheus": 2, "volcengine": 1}
    # Hour key format is "YYYY-MM-DD HH:00" (portable date()+extract bucketing).
    assert bucket["hour"].endswith(":00")


@pytest.mark.asyncio
async def test_ignores_events_older_than_the_24h_window(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    stale = utcnow() - timedelta(hours=30)
    async with session_factory.begin() as session:
        session.add_all([_event("prometheus", stale), _event("volcengine", stale)])

    async with session_factory() as session:
        assert await find_cross_source_spikes(session) == []

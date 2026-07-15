"""Tests for the silence-debt analytic (chronic no-expiry silences)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.datetime_utils import utcnow
from services.operations.silence_debt import get_silence_debt, summarize_silence_debt


async def _add_silence(session: AsyncSession, *, comment: str, expires_at: object | None, source: str = "volcengine"):
    from models import Silence

    silence = Silence(match_source=source, comment=comment, expires_at=expires_at)
    session.add(silence)
    await session.flush()
    return silence


async def _add_silenced_traces(session: AsyncSession, *, silence_id: int, count: int, age_days: float = 0.0) -> None:
    from models import DecisionTrace

    ts = utcnow() - timedelta(days=age_days)
    for i in range(count):
        session.add(
            DecisionTrace(
                # webhook_event_id is NOT NULL but irrelevant to the debt
                # aggregate (it groups by silence_id); a unique placeholder
                # keeps the rows distinct.
                webhook_event_id=silence_id * 100_000 + i,
                outcome="skipped",
                skip_code="silenced",
                silence_id=silence_id,
                created_at=ts,
            )
        )
    await session.flush()


@pytest.mark.asyncio
async def test_flags_chronic_no_expiry_silence(db_session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with db_session_factory.begin() as session:
        chronic = await _add_silence(session, comment="perm: GPU box", expires_at=None)
        await _add_silenced_traces(session, silence_id=int(chronic.id), count=600)

    async with db_session_factory() as session:
        debt = await get_silence_debt(session, window_days=30)

    assert debt["active_silences"] == 1
    assert debt["chronic_count"] == 1
    assert debt["total_suppressed"] == 600
    top = debt["silences"][0]
    assert top["chronic"] is True
    assert top["no_expiry"] is True
    assert top["daily_rate"] == round(600 / 30, 1)

    line = summarize_silence_debt(debt)
    assert line is not None and "Chronic silences" in line and "GPU box" in line


@pytest.mark.asyncio
async def test_expiring_and_low_volume_silences_are_not_chronic(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory.begin() as session:
        # No expiry but low volume → not chronic.
        quiet = await _add_silence(session, comment="quiet perm", expires_at=None)
        await _add_silenced_traces(session, silence_id=int(quiet.id), count=5)
        # High volume but has an expiry → a deliberate temporary mute, not debt.
        temp = await _add_silence(session, comment="24h trial", expires_at=utcnow() + timedelta(hours=24))
        await _add_silenced_traces(session, silence_id=int(temp.id), count=800)

    async with db_session_factory() as session:
        debt = await get_silence_debt(session, window_days=30)

    assert debt["chronic_count"] == 0
    assert summarize_silence_debt(debt) is None
    # Both silences still appear, ranked by volume, just not flagged chronic.
    assert [s["chronic"] for s in debt["silences"]] == [False, False]
    assert debt["silences"][0]["suppressed"] == 800


@pytest.mark.asyncio
async def test_window_excludes_old_suppressions(db_session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with db_session_factory.begin() as session:
        silence = await _add_silence(session, comment="perm", expires_at=None)
        await _add_silenced_traces(session, silence_id=int(silence.id), count=600, age_days=0.0)
        await _add_silenced_traces(session, silence_id=int(silence.id), count=600, age_days=45.0)

    async with db_session_factory() as session:
        debt = await get_silence_debt(session, window_days=30)

    # Only the in-window suppressions count; the 45-day-old batch is excluded.
    assert debt["total_suppressed"] == 600

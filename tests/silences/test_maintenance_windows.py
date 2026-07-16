"""Maintenance windows: occurrence math + sweep materialization (in-memory sqlite)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from models import MaintenanceWindow, Silence
from services.silences import store
from services.silences.maintenance_windows import (
    MAINTENANCE_CREATED_BY,
    active_occurrence,
    occurrence_marker,
    parse_days_of_week,
    sweep_maintenance_windows,
)


@pytest.fixture
async def session(db_session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with db_session_factory.begin() as sess:
        yield sess


@pytest.fixture(autouse=True)
def _reset_cache():
    store.invalidate_silences_cache()
    yield
    store.invalidate_silences_cache()


def _window(**over: object) -> MaintenanceWindow:
    base: dict[str, object] = {
        "name": "weekly-patch",
        "enabled": True,
        "match_source": "zabbix",
        "days_of_week": "7",  # Sunday
        "start_minute": 2 * 60,  # 02:00 local
        "duration_minutes": 120,
        "timezone": "Asia/Shanghai",
    }
    base.update(over)
    return MaintenanceWindow(**base)  # type: ignore[arg-type]


# 2026-07-19 is a Sunday. 02:30 Asia/Shanghai on Sunday = 18:30 UTC Saturday.
_INSIDE_SUNDAY_WINDOW_UTC = datetime(2026, 7, 18, 18, 30)


def test_parse_days_of_week_validates() -> None:
    assert parse_days_of_week("1, 7") == frozenset({1, 7})
    with pytest.raises(ValueError):
        parse_days_of_week("0,3")
    with pytest.raises(ValueError):
        parse_days_of_week("")


def test_occurrence_inside_window() -> None:
    occ = active_occurrence(_window(), _INSIDE_SUNDAY_WINDOW_UTC)
    assert occ is not None
    assert occ.occurrence_date == date(2026, 7, 19)
    # 02:00–04:00 CST == 18:00–20:00 UTC the previous day.
    assert occ.starts_at == datetime(2026, 7, 18, 18, 0)
    assert occ.ends_at == datetime(2026, 7, 18, 20, 0)


def test_occurrence_outside_window_or_wrong_day() -> None:
    # 01:30 CST Sunday — before the window opens.
    assert active_occurrence(_window(), datetime(2026, 7, 18, 17, 30)) is None
    # Right time of day, but a Wednesday.
    assert active_occurrence(_window(), datetime(2026, 7, 21, 18, 30)) is None


def test_occurrence_crossing_midnight_belongs_to_start_day() -> None:
    # Saturday 23:00 CST + 4h runs into Sunday 01:00 CST; that instant is
    # still the Saturday occurrence.
    window = _window(days_of_week="6", start_minute=23 * 60, duration_minutes=240)
    occ = active_occurrence(window, datetime(2026, 7, 18, 17, 0))  # Sun 01:00 CST
    assert occ is not None
    assert occ.occurrence_date == date(2026, 7, 18)


@pytest.mark.asyncio
async def test_sweep_materializes_active_window_idempotently(session: AsyncSession) -> None:
    window = _window()
    session.add(window)
    await session.flush()

    result = await sweep_maintenance_windows(session, now=_INSIDE_SUNDAY_WINDOW_UTC)
    assert result == {"created": 1, "lifted": 0}

    silences = list((await session.execute(select(Silence))).scalars().all())
    assert len(silences) == 1
    created = silences[0]
    assert created.created_by == MAINTENANCE_CREATED_BY
    assert created.match_source == "zabbix"
    assert created.comment.startswith(occurrence_marker(int(window.id), date(2026, 7, 19)))
    assert created.expires_at == datetime(2026, 7, 18, 20, 0)

    # Second sweep of the same occurrence is a no-op.
    result = await sweep_maintenance_windows(session, now=_INSIDE_SUNDAY_WINDOW_UTC)
    assert result == {"created": 0, "lifted": 0}
    assert len(list((await session.execute(select(Silence))).scalars().all())) == 1


@pytest.mark.asyncio
async def test_sweep_ignores_disabled_and_inactive_windows(session: AsyncSession) -> None:
    session.add(_window(name="disabled", enabled=False))
    session.add(_window(name="wrong-day", days_of_week="3"))
    await session.flush()
    result = await sweep_maintenance_windows(session, now=_INSIDE_SUNDAY_WINDOW_UTC)
    assert result == {"created": 0, "lifted": 0}


@pytest.mark.asyncio
async def test_sweep_lifts_silence_when_window_disabled(session: AsyncSession) -> None:
    window = _window()
    session.add(window)
    await session.flush()
    await sweep_maintenance_windows(session, now=_INSIDE_SUNDAY_WINDOW_UTC)

    window.enabled = False
    await session.flush()
    result = await sweep_maintenance_windows(session, now=_INSIDE_SUNDAY_WINDOW_UTC)
    assert result == {"created": 0, "lifted": 1}
    silence = (await session.execute(select(Silence))).scalars().one()
    assert silence.lifted_at is not None


@pytest.mark.asyncio
async def test_sweep_lifts_silence_when_window_deleted(session: AsyncSession) -> None:
    window = _window()
    session.add(window)
    await session.flush()
    await sweep_maintenance_windows(session, now=_INSIDE_SUNDAY_WINDOW_UTC)

    await session.delete(window)
    await session.flush()
    result = await sweep_maintenance_windows(session, now=_INSIDE_SUNDAY_WINDOW_UTC)
    assert result == {"created": 0, "lifted": 1}


@pytest.mark.asyncio
async def test_operator_lifted_occurrence_is_not_resurrected(session: AsyncSession) -> None:
    """Lifting a maintenance silence by hand must stick for that occurrence."""
    window = _window()
    session.add(window)
    await session.flush()
    await sweep_maintenance_windows(session, now=_INSIDE_SUNDAY_WINDOW_UTC)

    silence = (await session.execute(select(Silence))).scalars().one()
    await store.lift_silence(session=session, silence_id=int(silence.id))

    result = await sweep_maintenance_windows(session, now=_INSIDE_SUNDAY_WINDOW_UTC)
    assert result == {"created": 0, "lifted": 0}


@pytest.mark.asyncio
async def test_sweep_skips_window_with_invalid_schedule(session: AsyncSession) -> None:
    session.add(_window(name="bad", days_of_week="9"))
    session.add(_window(name="good"))
    await session.flush()
    result = await sweep_maintenance_windows(session, now=_INSIDE_SUNDAY_WINDOW_UTC)
    assert result == {"created": 1, "lifted": 0}

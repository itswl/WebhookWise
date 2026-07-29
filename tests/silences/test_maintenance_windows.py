"""Maintenance windows: occurrence math + sweep materialization (in-memory sqlite)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from models import MaintenanceWindow, Silence
from services.silences import maintenance_windows as mw
from services.silences import store
from services.silences.maintenance_windows import (
    MAINTENANCE_CREATED_BY,
    active_occurrence,
    occurrence_marker,
    parse_days_of_week,
    schedule_digest,
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


def test_occurrence_keeps_real_length_across_dst_gap() -> None:
    """Spring-forward: 2026-03-08 America/New_York has no 02:xx wall hour.

    A 02:30+60min window must not collapse to zero length — the start
    normalizes forward and the end is start + duration in absolute time.
    """
    window = _window(
        days_of_week="7",  # 2026-03-08 is a Sunday
        start_minute=2 * 60 + 30,
        duration_minutes=60,
        timezone="America/New_York",
    )
    # 03:30 EDT == 07:30 UTC; probe mid-window at 07:45 UTC.
    occ = active_occurrence(window, datetime(2026, 3, 8, 7, 45))
    assert occ is not None
    assert (occ.ends_at - occ.starts_at) == timedelta(minutes=60)


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
    assert created.mw_window_id == int(window.id)
    assert created.mw_occurrence_date == "2026-07-19"
    assert created.mw_schedule_digest == schedule_digest(window)

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
    assert silence.mw_window_id is None  # identity cleared on retire


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
async def test_extending_live_window_takes_effect_in_one_sweep(session: AsyncSession) -> None:
    """P0 regression: editing a live window retires the old occurrence and
    materializes the new schedule in the SAME sweep."""
    window = _window()  # 02:00–04:00 CST
    session.add(window)
    await session.flush()
    await sweep_maintenance_windows(session, now=_INSIDE_SUNDAY_WINDOW_UTC)

    window.duration_minutes = 240  # extend to 02:00–06:00 CST
    await session.flush()
    result = await sweep_maintenance_windows(session, now=_INSIDE_SUNDAY_WINDOW_UTC)
    assert result == {"created": 1, "lifted": 1}

    rows = list((await session.execute(select(Silence).order_by(Silence.id))).scalars().all())
    assert len(rows) == 2
    retired, current = rows
    assert retired.lifted_at is not None and retired.mw_window_id is None
    assert current.lifted_at is None
    assert current.expires_at == datetime(2026, 7, 18, 22, 0)  # 06:00 CST
    assert current.mw_schedule_digest == schedule_digest(window)


@pytest.mark.asyncio
async def test_moving_window_later_rematerializes_when_it_opens(session: AsyncSession) -> None:
    """P0 regression: moving a live window later mutes again once the new
    schedule opens, instead of being blocked by its own lifted row."""
    window = _window()  # 02:00–04:00 CST
    session.add(window)
    await session.flush()
    await sweep_maintenance_windows(session, now=_INSIDE_SUNDAY_WINDOW_UTC)  # 02:30 CST

    window.start_minute = 3 * 60  # move to 03:00–05:00 CST
    await session.flush()
    # Sweep at 02:40 CST: new schedule not open yet → old occurrence retired.
    result = await sweep_maintenance_windows(session, now=datetime(2026, 7, 18, 18, 40))
    assert result == {"created": 0, "lifted": 1}
    # Sweep at 03:30 CST: new schedule open → re-materialized.
    result = await sweep_maintenance_windows(session, now=datetime(2026, 7, 18, 19, 30))
    assert result == {"created": 1, "lifted": 0}
    live = [s for s in (await session.execute(select(Silence))).scalars().all() if s.lifted_at is None]
    assert len(live) == 1
    assert live[0].expires_at == datetime(2026, 7, 18, 21, 0)  # 05:00 CST


@pytest.mark.asyncio
async def test_reenabling_window_rematerializes_same_day(session: AsyncSession) -> None:
    window = _window()
    session.add(window)
    await session.flush()
    await sweep_maintenance_windows(session, now=_INSIDE_SUNDAY_WINDOW_UTC)

    window.enabled = False
    await session.flush()
    await sweep_maintenance_windows(session, now=_INSIDE_SUNDAY_WINDOW_UTC)

    window.enabled = True
    await session.flush()
    result = await sweep_maintenance_windows(session, now=_INSIDE_SUNDAY_WINDOW_UTC)
    assert result == {"created": 1, "lifted": 0}


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
async def test_concurrent_duplicate_insert_is_swallowed(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A racing sweep's identical INSERT degrades to a skipped unique violation."""
    window = _window()
    session.add(window)
    await session.flush()
    await sweep_maintenance_windows(session, now=_INSIDE_SUNDAY_WINDOW_UTC)

    async def never_tracked(*args: object, **kwargs: object) -> bool:
        return False  # simulate the check racing ahead of a concurrent insert

    monkeypatch.setattr(mw, "_occurrence_already_tracked", never_tracked)
    result = await sweep_maintenance_windows(session, now=_INSIDE_SUNDAY_WINDOW_UTC)
    assert result == {"created": 0, "lifted": 0}
    assert len(list((await session.execute(select(Silence))).scalars().all())) == 1


@pytest.mark.asyncio
async def test_comment_edit_no_longer_breaks_occurrence_tracking(session: AsyncSession) -> None:
    """The comment is a label; identity lives in columns."""
    window = _window()
    session.add(window)
    await session.flush()
    await sweep_maintenance_windows(session, now=_INSIDE_SUNDAY_WINDOW_UTC)

    silence = (await session.execute(select(Silence))).scalars().one()
    silence.comment = "operator scribbled over this"
    await session.flush()

    result = await sweep_maintenance_windows(session, now=_INSIDE_SUNDAY_WINDOW_UTC)
    assert result == {"created": 0, "lifted": 0}
    refreshed = (await session.execute(select(Silence))).scalars().one()
    assert refreshed.lifted_at is None  # still live, not orphan-lifted


@pytest.mark.asyncio
async def test_sweep_skips_window_with_invalid_schedule(session: AsyncSession) -> None:
    session.add(_window(name="bad", days_of_week="9"))
    session.add(_window(name="good"))
    await session.flush()
    result = await sweep_maintenance_windows(session, now=_INSIDE_SUNDAY_WINDOW_UTC)
    assert result == {"created": 1, "lifted": 0}

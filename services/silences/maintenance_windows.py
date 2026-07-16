"""Maintenance windows: recurring silence schedules materialized as silences.

A MaintenanceWindow row is pure schedule + match criteria; it never matches
alerts directly. The scheduler sweep (`run_maintenance_window_sweep`) turns the
currently-active occurrence of each enabled window into a normal expiring
Silence row, tagged so it is recognizable and idempotent:

- created_by = "maintenance-window"
- comment starts with "[mw:{window_id}:{occurrence_date}]"

Everything downstream (forward-decision cache, suppression accounting, the
debt report) keeps operating on plain silences. When a window is disabled or
deleted mid-occurrence, the sweep lifts its live silence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utcnow
from core.logger import get_logger
from db.session import session_scope
from models import MaintenanceWindow, Silence
from services.silences.store import create_silence, lift_silence

logger = get_logger("silences.maintenance_windows")

MAINTENANCE_CREATED_BY = "maintenance-window"

_MARKER_PREFIX = "[mw:"


def occurrence_marker(window_id: int, occurrence_date: date) -> str:
    """Deterministic comment prefix identifying one occurrence of one window."""
    return f"{_MARKER_PREFIX}{window_id}:{occurrence_date.isoformat()}]"


def parse_days_of_week(raw: str) -> frozenset[int]:
    """Parse the CSV of ISO weekday numbers (1=Monday … 7=Sunday)."""
    days: set[int] = set()
    for part in str(raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        value = int(part)
        if not 1 <= value <= 7:
            raise ValueError(f"day of week must be 1..7, got {value}")
        days.add(value)
    if not days:
        raise ValueError("days_of_week must contain at least one ISO weekday (1..7)")
    return frozenset(days)


@dataclass(frozen=True, slots=True)
class WindowOccurrence:
    """One concrete occurrence of a window, in UTC."""

    occurrence_date: date  # local date the window STARTS on
    starts_at: datetime  # naive UTC, matching Silence.expires_at storage
    ends_at: datetime  # naive UTC


def _window_tz(window: MaintenanceWindow) -> ZoneInfo:
    try:
        return ZoneInfo(str(window.timezone or "Asia/Shanghai"))
    except ZoneInfoNotFoundError:
        logger.warning(
            "[MaintenanceWindow] Unknown timezone %r on window id=%s, falling back to UTC",
            window.timezone,
            window.id,
        )
        return ZoneInfo("UTC")


def active_occurrence(window: MaintenanceWindow, now: datetime) -> WindowOccurrence | None:
    """Return the occurrence covering `now`, or None.

    `now` is naive UTC (the project's storage convention). A window may cross
    local midnight, so both today's and yesterday's start are candidates; the
    occurrence date is the local day the window starts on.
    """
    days = parse_days_of_week(window.days_of_week)
    tz = _window_tz(window)
    now_utc = now.replace(tzinfo=UTC)
    local_today = now_utc.astimezone(tz).date()
    for candidate in (local_today, local_today - timedelta(days=1)):
        if candidate.isoweekday() not in days:
            continue
        local_start = datetime.combine(candidate, time(0, 0), tzinfo=tz) + timedelta(minutes=int(window.start_minute))
        local_end = local_start + timedelta(minutes=int(window.duration_minutes))
        if local_start <= now_utc < local_end:
            return WindowOccurrence(
                occurrence_date=candidate,
                starts_at=local_start.astimezone(UTC).replace(tzinfo=None),
                ends_at=local_end.astimezone(UTC).replace(tzinfo=None),
            )
    return None


async def _live_maintenance_silences(session: AsyncSession, now: datetime) -> list[Silence]:
    stmt = select(Silence).where(
        Silence.created_by == MAINTENANCE_CREATED_BY,
        Silence.lifted_at.is_(None),
        Silence.expires_at.isnot(None),
        Silence.expires_at > now,
    )
    return list((await session.execute(stmt)).scalars().all())


def _marker_window_id(comment: str) -> int | None:
    """Extract the window id from an occurrence marker, None when unparseable."""
    text = str(comment or "")
    if not text.startswith(_MARKER_PREFIX):
        return None
    head = text[len(_MARKER_PREFIX) :].split("]", 1)[0]
    window_part = head.split(":", 1)[0]
    return int(window_part) if window_part.isdigit() else None


async def sweep_maintenance_windows(session: AsyncSession, *, now: datetime | None = None) -> dict[str, int]:
    """Materialize active occurrences into silences; lift orphaned ones.

    Idempotent: an occurrence is created at most once (marker lookup) and a
    lifted-by-operator silence is not resurrected for the same occurrence,
    because the marker lookup also matches lifted rows.
    """
    now = now or utcnow()
    windows = list((await session.execute(select(MaintenanceWindow))).scalars().all())
    live = await _live_maintenance_silences(session, now)

    created = 0
    lifted = 0

    active_markers: set[str] = set()
    windows_by_id = {int(w.id): w for w in windows}
    for window in windows:
        if not window.enabled:
            continue
        try:
            occurrence = active_occurrence(window, now)
        except ValueError as e:
            logger.warning("[MaintenanceWindow] Invalid schedule on window id=%s: %s", window.id, e)
            continue
        if occurrence is None:
            continue
        marker = occurrence_marker(int(window.id), occurrence.occurrence_date)
        active_markers.add(marker)
        existing = (
            await session.execute(select(Silence.id).where(Silence.comment.like(f"{marker}%")).limit(1))
        ).scalar_one_or_none()
        if existing is not None:
            continue
        label = f"{marker} {window.name}"[:500]
        await create_silence(
            session=session,
            match_source=window.match_source,
            match_importance=window.match_importance,
            match_event_type=window.match_event_type,
            match_project=window.match_project,
            match_region=window.match_region,
            match_environment=window.match_environment,
            match_payload=window.match_payload,
            comment=label,
            created_by=MAINTENANCE_CREATED_BY,
            expires_at=occurrence.ends_at,
        )
        created += 1
        logger.info(
            "[MaintenanceWindow] Materialized occurrence window=%s (%s) until %s",
            window.id,
            window.name,
            occurrence.ends_at.isoformat(),
        )

    # Lift live maintenance silences whose window is gone, disabled, or whose
    # occurrence is no longer the active one (schedule edited mid-window).
    for silence in live:
        marker = str(silence.comment or "").split("]", 1)[0] + "]" if silence.comment else ""
        window_id = _marker_window_id(silence.comment)
        parent = windows_by_id.get(window_id) if window_id is not None else None
        if parent is not None and parent.enabled and marker in active_markers:
            continue
        await lift_silence(session=session, silence_id=int(silence.id))
        lifted += 1
        logger.info(
            "[MaintenanceWindow] Lifted orphaned maintenance silence id=%s (window %s)",
            silence.id,
            window_id if window_id is not None else "unknown",
        )

    return {"created": created, "lifted": lifted}


async def run_maintenance_window_sweep() -> dict[str, int]:
    """Scheduler entry point: sweep in its own transaction."""
    async with session_scope() as session:
        result = await sweep_maintenance_windows(session)
        await session.commit()
        return result

"""Maintenance windows: recurring silence schedules materialized as silences.

A MaintenanceWindow row is pure schedule + match criteria; it never matches
alerts directly. The scheduler sweep (`run_maintenance_window_sweep`) turns the
currently-active occurrence of each enabled window into a normal expiring
Silence row. The occurrence identity lives in real columns —
(mw_window_id, mw_occurrence_date, mw_schedule_digest) — under a partial
unique index:

- The SCHEDULE DIGEST makes an edited window a new identity: the sweep retires
  the old occurrence's silence (lift + clear identity columns) and materializes
  the new schedule in the same pass, so a mid-occurrence edit takes effect
  immediately instead of being blocked by its own lifted row.
- The unique index makes concurrent sweeps (scheduler + the API-mutation
  sweeps) race to a unique violation that is swallowed, never a duplicate.
- An OPERATOR-lifted occurrence keeps its identity columns, so the sweep will
  not resurrect a silence a human deliberately lifted (for that schedule).

Occurrence ends are computed by UTC arithmetic (start instant + duration), so
a window straddling a DST transition keeps its real length instead of
collapsing to zero on spring-forward days.

Everything downstream (forward-decision cache, suppression accounting, the
debt report) keeps operating on plain silences. The `[mw:...]` comment prefix
is retained purely as a human-readable label; no logic parses it anymore.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utcnow
from core.logger import get_logger
from db.session import session_scope
from models import MaintenanceWindow, Silence
from services.silences.store import invalidate_silences_cache, publish_silences_invalidation

logger = get_logger("silences.maintenance_windows")

MAINTENANCE_CREATED_BY = "maintenance-window"


def occurrence_marker(window_id: int, occurrence_date: date) -> str:
    """Human-readable comment prefix for a materialized occurrence."""
    return f"[mw:{window_id}:{occurrence_date.isoformat()}]"


def schedule_digest(window: MaintenanceWindow) -> str:
    """Digest of the schedule fields that define an occurrence's identity."""
    raw = f"{window.days_of_week}|{window.start_minute}|{window.duration_minutes}|{window.timezone}"
    return hashlib.sha1(raw.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]


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
    occurrence date is the local day the window starts on. The END is the
    start instant plus the duration in absolute time, so DST transitions
    stretch or shrink the local wall-clock but never the real length (and a
    start falling into a spring-forward gap normalizes forward instead of
    producing a zero-length window).
    """
    days = parse_days_of_week(window.days_of_week)
    tz = _window_tz(window)
    now_utc = now.replace(tzinfo=UTC)
    local_today = now_utc.astimezone(tz).date()
    for candidate in (local_today, local_today - timedelta(days=1)):
        if candidate.isoweekday() not in days:
            continue
        local_start = datetime.combine(candidate, time(0, 0), tzinfo=tz) + timedelta(minutes=int(window.start_minute))
        starts_utc = local_start.astimezone(UTC)
        ends_utc = starts_utc + timedelta(minutes=int(window.duration_minutes))
        if starts_utc <= now_utc < ends_utc:
            return WindowOccurrence(
                occurrence_date=candidate,
                starts_at=starts_utc.replace(tzinfo=None),
                ends_at=ends_utc.replace(tzinfo=None),
            )
    return None


async def _live_maintenance_silences(session: AsyncSession, now: datetime) -> list[Silence]:
    stmt = select(Silence).where(
        Silence.created_by == MAINTENANCE_CREATED_BY,
        Silence.mw_window_id.isnot(None),
        Silence.lifted_at.is_(None),
        Silence.expires_at.isnot(None),
        Silence.expires_at > now,
    )
    return list((await session.execute(stmt)).scalars().all())


async def _occurrence_already_tracked(session: AsyncSession, window_id: int, occurrence_date: str, digest: str) -> bool:
    """Whether this exact occurrence identity exists (live OR operator-lifted)."""
    stmt = (
        select(Silence.id)
        .where(
            Silence.mw_window_id == window_id,
            Silence.mw_occurrence_date == occurrence_date,
            Silence.mw_schedule_digest == digest,
        )
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def sweep_maintenance_windows(session: AsyncSession, *, now: datetime | None = None) -> dict[str, int]:
    """Materialize active occurrences into silences; retire stale ones.

    Idempotent and race-safe: the occurrence identity columns are covered by a
    partial unique index, so a concurrent sweep's duplicate INSERT collapses
    into a swallowed unique violation. Retiring (window disabled/deleted, or
    schedule edited mid-occurrence) lifts the silence AND clears its identity
    columns, so the same window/day can re-materialize under a new schedule —
    while an operator-lifted row keeps its identity and stays lifted.
    """
    now = now or utcnow()
    windows = list((await session.execute(select(MaintenanceWindow))).scalars().all())
    live = await _live_maintenance_silences(session, now)

    created = 0
    lifted = 0

    active_keys: set[tuple[int, str, str]] = set()
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
        window_id = int(window.id)
        occ_date = occurrence.occurrence_date.isoformat()
        digest = schedule_digest(window)
        active_keys.add((window_id, occ_date, digest))
        if await _occurrence_already_tracked(session, window_id, occ_date, digest):
            continue
        label = f"{occurrence_marker(window_id, occurrence.occurrence_date)} {window.name}"[:500]
        silence = Silence(
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
            mw_window_id=window_id,
            mw_occurrence_date=occ_date,
            mw_schedule_digest=digest,
        )
        # Nested SAVEPOINT so a concurrent sweep's identical INSERT degrades to
        # a swallowed unique violation without poisoning the outer transaction.
        try:
            async with session.begin_nested():
                session.add(silence)
                await session.flush()
        except IntegrityError:
            logger.info(
                "[MaintenanceWindow] Occurrence already materialized concurrently window=%s date=%s",
                window_id,
                occ_date,
            )
            continue
        created += 1
        logger.info(
            "[MaintenanceWindow] Materialized occurrence window=%s (%s) until %s",
            window_id,
            window.name,
            occurrence.ends_at.isoformat(),
        )

    # Retire live occurrence silences whose identity is no longer active: the
    # window is gone, disabled, or its schedule was edited (new digest). The
    # identity columns are cleared so the new schedule can materialize.
    for silence in live:
        key = (
            int(silence.mw_window_id or 0),
            str(silence.mw_occurrence_date or ""),
            str(silence.mw_schedule_digest or ""),
        )
        if key in active_keys:
            continue
        silence.lifted_at = now
        silence.mw_window_id = None
        silence.mw_occurrence_date = None
        silence.mw_schedule_digest = None
        lifted += 1
        logger.info("[MaintenanceWindow] Retired maintenance silence id=%s (window %s)", silence.id, key[0])

    if created or lifted:
        await session.flush()
        invalidate_silences_cache()
        await publish_silences_invalidation()

    return {"created": created, "lifted": lifted}


async def run_maintenance_window_sweep() -> dict[str, int]:
    """Scheduler entry point: sweep in its own transaction."""
    async with session_scope() as session:
        result = await sweep_maintenance_windows(session)
        await session.commit()
        return result

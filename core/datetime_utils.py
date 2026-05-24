"""UTC-aware datetime helpers.

PostgreSQL preserves timezone info, but SQLite strips it on round-trip.
Use `utcnow()` for new timestamps and `ensure_utc()` when reading from DB.
"""

from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def ensure_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt

"""UTC-aware datetime helpers.

All DB columns are ``timestamp without time zone``.  asyncpg rejects
tz-aware Python values against such columns, and SQLite strips tzinfo on
round-trip.  Use ``utcnow()`` (returns a *naive* UTC datetime) for all DB
writes and query parameters to stay compatible with both backends.
"""

from datetime import datetime, timezone


def utcnow() -> datetime:
    """Return a naive UTC datetime, safe for all DB columns."""
    return datetime.now(tz=timezone.utc).replace(tzinfo=None)


def ensure_utc(dt: datetime | None) -> datetime | None:
    """Normalize a DB-read datetime to tz-aware UTC (for display / external APIs)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt

"""Naive UTC datetime helpers for database writes, queries, and API output."""

from datetime import UTC, datetime


def utcnow() -> datetime:
    """Return a naive UTC datetime, safe for all DB columns."""
    return datetime.now(tz=UTC).replace(tzinfo=None)


def utc_isoformat(value: datetime | None) -> str | None:
    """Serialize a datetime as explicit UTC for browser-safe API responses."""
    if value is None:
        return None
    value = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return value.isoformat().replace("+00:00", "Z")

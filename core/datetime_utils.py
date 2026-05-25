"""Naive UTC datetime helpers for database writes, queries, and API output."""

from datetime import UTC, datetime


def utcnow() -> datetime:
    """Return a naive UTC datetime, safe for all DB columns."""
    return datetime.now(tz=UTC).replace(tzinfo=None)


def naive_utc(value: datetime) -> datetime:
    """Normalize aware datetimes to naive UTC; treat naive inputs as UTC."""
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def parse_utc_datetime(value: str | None) -> datetime | None:
    """Parse an ISO datetime string into the project's naive-UTC convention."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return naive_utc(parsed)


def utc_isoformat(value: datetime | None) -> str | None:
    """Serialize a datetime as explicit UTC for browser-safe API responses."""
    if value is None:
        return None
    value = naive_utc(value).replace(tzinfo=UTC)
    return value.isoformat().replace("+00:00", "Z")

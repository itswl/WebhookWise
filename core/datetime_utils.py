"""Naive UTC datetime helpers for database writes and query parameters."""

from datetime import UTC, datetime


def utcnow() -> datetime:
    """Return a naive UTC datetime, safe for all DB columns."""
    return datetime.now(tz=UTC).replace(tzinfo=None)

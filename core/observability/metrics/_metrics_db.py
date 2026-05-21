"""Database metrics."""

from __future__ import annotations

from core.observability.metrics.base import Counter, Gauge, Histogram

DATABASE_EVENTS_COUNT = Gauge("webhook.events.count", "Current number of webhook events in active table")
DB_POOL_CHECKED_OUT = Gauge("db.pool.connections.checked_out", "Checked-out database connections")
DB_POOL_SIZE = Gauge("db.pool.connections.max", "Database connection pool capacity")
DB_SESSION_TOTAL = Counter(
    "db.sessions",
    "Database session/transaction lifecycle count",
    ("db.operation", "db.status"),
)
DB_SESSION_DURATION_SECONDS = Histogram(
    "db.session.duration",
    "Database session/transaction duration",
    ("db.operation", "db.status"),
    unit="s",
)

"""Shared per-source event aggregates (the one GROUP BY, three consumers).

source_health and alert_quality each carried their own copy of the same
windowed aggregation over webhook_events (total / duplicates / last_seen);
this module is the single implementation. rule_audit intentionally stays
separate: its dimension is (source, RULE NAME extracted from the payload),
which is a different granularity, not a duplicate of this query.

Read-only; no new instruments.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import Integer, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from core.datetime_utils import utcnow
from models import WebhookEvent


@dataclass(frozen=True, slots=True)
class SourceEventStats:
    source: str
    source_connection_id: int | None
    total: int
    duplicates: int
    last_seen: datetime | None


async def get_source_event_stats(
    session: AsyncSession,
    *,
    window_days: int,
    granularity: Literal["source", "connection"] = "source",
) -> list[SourceEventStats]:
    """Windowed totals per source (or per source+connection)."""
    window_days = max(1, int(window_days))
    start = utcnow() - timedelta(days=window_days)

    group_cols: list[InstrumentedAttribute[Any]] = [WebhookEvent.source]
    if granularity == "connection":
        group_cols.append(WebhookEvent.source_connection_id)

    stmt = (
        select(
            *group_cols,
            func.count(WebhookEvent.id).label("total"),
            func.sum(WebhookEvent.is_duplicate.cast(Integer)).label("duplicates"),
            func.max(WebhookEvent.timestamp).label("last_seen"),
        )
        .where(WebhookEvent.timestamp >= start)
        .group_by(*group_cols)
        .order_by(func.count(WebhookEvent.id).desc())
    )
    rows = (await session.execute(stmt)).all()

    stats: list[SourceEventStats] = []
    for row in rows:
        if granularity == "connection":
            source, connection_id, total, duplicates, last_seen = row
        else:
            source, total, duplicates, last_seen = row
            connection_id = None
        stats.append(
            SourceEventStats(
                source=str(source or "unknown").strip(),
                source_connection_id=int(connection_id) if connection_id is not None else None,
                total=int(total or 0),
                duplicates=int(duplicates or 0),
                last_seen=last_seen,
            )
        )
    return stats

"""Cross-source correlation — detect alerts from different sources that fire
close together and may be related (e.g. volcengine GPU + prometheus CPU).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import extract, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utcnow
from models import WebhookEvent

_MAX_PAIRS = 20


async def find_cross_source_spikes(session: AsyncSession, *, limit: int = _MAX_PAIRS) -> list[dict[str, Any]]:
    """Find recent hours where multiple sources fired alerts simultaneously."""
    start = utcnow() - timedelta(hours=24)

    # date() + extract("hour") instead of the PostgreSQL-only date_trunc so the
    # bucketing also works on the SQLite test shim (same convention as
    # analysis_queries' func.date usage).
    day = func.date(WebhookEvent.timestamp).label("day")
    hour_of_day = extract("hour", WebhookEvent.timestamp).label("hour_of_day")
    rows = (
        await session.execute(
            select(
                day,
                hour_of_day,
                WebhookEvent.source,
                func.count(WebhookEvent.id).label("cnt"),
            )
            .where(WebhookEvent.timestamp >= start)
            .group_by(day, hour_of_day, WebhookEvent.source)
            .order_by(day.desc(), hour_of_day.desc())
        )
    ).all()

    # Group by hour — buckets with 2+ sources are cross-source events.
    by_hour: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        hour_key = f"{row.day} {int(row.hour_of_day):02d}:00"
        by_hour.setdefault(hour_key, []).append({"source": str(row.source or "unknown"), "count": int(row.cnt)})

    results: list[dict[str, Any]] = []
    for hour_key, sources in sorted(by_hour.items(), reverse=True):
        if len(sources) >= 2:
            results.append({"hour": hour_key, "sources": sources[:5], "source_count": len(sources)})
        if len(results) >= limit:
            break

    return results

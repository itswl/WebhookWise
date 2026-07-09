"""Cross-source correlation — detect alerts from different sources that fire
close together and may be related (e.g. volcengine GPU + prometheus CPU).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utcnow
from models import WebhookEvent

_MAX_PAIRS = 20


async def find_cross_source_spikes(
    session: AsyncSession, *, limit: int = _MAX_PAIRS
) -> list[dict[str, Any]]:
    """Find recent hours where multiple sources fired alerts simultaneously."""
    start = utcnow() - timedelta(hours=24)

    rows = (
        await session.execute(
            select(
                func.date_trunc("hour", WebhookEvent.timestamp).label("hour"),
                WebhookEvent.source,
                func.count(WebhookEvent.id).label("cnt"),
            )
            .where(WebhookEvent.timestamp >= start)
            .group_by("hour", WebhookEvent.source)
            .having(func.count(WebhookEvent.id) >= 1)
            .order_by(func.date_trunc("hour", WebhookEvent.timestamp).desc())
        )
    ).all()

    # Group by hour — buckets with 2+ sources are cross-source events.
    by_hour: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        hour_key = str(row[0])
        by_hour.setdefault(hour_key, []).append(
            {"source": str(row[1] or "unknown"), "count": int(row[2])}
        )

    results: list[dict[str, Any]] = []
    for hour_key, sources in sorted(by_hour.items(), reverse=True):
        if len(sources) >= 2:
            results.append(
                {"hour": hour_key, "sources": sources[:5], "source_count": len(sources)}
            )
        if len(results) >= limit:
            break

    return results

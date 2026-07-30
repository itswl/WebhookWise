"""Source health aggregation — per-source volume, dedup rate, forward rate, and recency.

Read-only GROUP BY over webhook_events + decision_trace. No new instruments.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utcnow
from models import DecisionTrace
from services.webhooks.source_stats import get_source_event_stats


async def get_source_health(session: AsyncSession, *, window_days: int = 7) -> list[dict[str, Any]]:
    """Per-source health snapshot over *window_days*."""
    window_days = max(1, int(window_days))
    start = utcnow() - timedelta(days=window_days)

    rows = await get_source_event_stats(session, window_days=window_days, granularity="source")

    if not rows:
        return []

    # Forward rate per source from decision_trace (same window).
    fwd_rows = (
        await session.execute(
            select(
                DecisionTrace.source,
                func.count(DecisionTrace.id),
            )
            .where(
                DecisionTrace.created_at >= start,
                DecisionTrace.outcome == "forwarded",
                DecisionTrace.source.isnot(None),
            )
            .group_by(DecisionTrace.source)
        )
    ).all()
    forwarded_by_source: dict[str, int] = {}
    for source, count in fwd_rows:
        forwarded_by_source[str(source or "").strip()] = int(count or 0)

    results: list[dict[str, Any]] = []
    for row in rows:
        source = row.source
        total = row.total
        duplicates = row.duplicates
        last_seen = row.last_seen
        forwarded = forwarded_by_source.get(source, 0)

        dup_pct = round(100.0 * duplicates / total, 1) if total else 0.0
        fwd_pct = round(100.0 * forwarded / total, 1) if total else 0.0
        results.append(
            {
                "source": source,
                "total": total,
                "duplicates": duplicates,
                "duplicate_pct": dup_pct,
                "forwarded": forwarded,
                "forward_pct": fwd_pct,
                "last_seen": last_seen.isoformat() if last_seen is not None else None,
            }
        )

    return results

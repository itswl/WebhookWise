"""Silence rule historical backtesting service."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utcnow
from models import WebhookEvent
from services.webhooks.decisioning import SilenceSnapshot, match_active_silence


async def backtest_silence_rule(
    session: AsyncSession,
    *,
    match_source: str = "",
    match_importance: str = "",
    match_event_type: str = "",
    match_project: str = "",
    match_region: str = "",
    match_environment: str = "",
    match_payload: str = "",
    lookback_days: int = 30,
    limit: int = 20,
) -> dict[str, Any]:
    """Test a proposed silence rule against historical webhook events.

    Scans events in the specified lookback window and reports how many would have
    been silenced, breaking results down by source and importance.
    """
    lookback_days = max(1, min(int(lookback_days), 90))
    start_time = utcnow() - timedelta(days=lookback_days)

    # Query historical events in range
    stmt = (
        select(WebhookEvent)
        .where(WebhookEvent.timestamp >= start_time)
        .order_by(WebhookEvent.timestamp.desc())
    )
    result = await session.execute(stmt)
    events = result.scalars().all()

    # Create the silence snapshot representation
    silence_snap = SilenceSnapshot(
        id=999999,  # Placeholder ID
        match_source=match_source,
        match_importance=match_importance,
        match_event_type=match_event_type,
        match_project=match_project,
        match_region=match_region,
        match_environment=match_environment,
        match_payload=match_payload,
    )

    total_scanned = len(events)
    total_matched = 0
    importance_counts: dict[str, int] = {"high": 0, "medium": 0, "low": 0, "unknown": 0}
    source_counts: dict[str, int] = {}
    sample_matched_events: list[dict[str, Any]] = []

    for event in events:
        # Match using the pipeline's exact same matching logic
        matched = match_active_silence(
            [silence_snap],
            event_type="",  # Default
            importance=event.importance or "unknown",
            source=event.source,
            is_duplicate=event.is_duplicate,
            parsed_data=event.parsed_data,
        )
        if matched:
            total_matched += 1
            imp = event.importance or "unknown"
            importance_counts[imp] = importance_counts.get(imp, 0) + 1
            source_counts[event.source] = source_counts.get(event.source, 0) + 1

            if len(sample_matched_events) < limit:
                summary = ""
                if event.parsed_data:
                    summary = str(
                        event.parsed_data.get("summary")
                        or event.parsed_data.get("RuleName")
                        or event.parsed_data.get("alertname")
                        or ""
                    )
                sample_matched_events.append(
                    {
                        "id": event.id,
                        "timestamp": event.timestamp.isoformat() if event.timestamp else "",
                        "source": event.source,
                        "importance": imp,
                        "is_duplicate": event.is_duplicate,
                        "summary": summary[:120],
                    }
                )

    return {
        "total_scanned": total_scanned,
        "total_matched": total_matched,
        "importance_counts": importance_counts,
        "source_counts": source_counts,
        "sample_matched_events": sample_matched_events,
    }

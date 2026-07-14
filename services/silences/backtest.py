"""Silence rule historical backtesting service."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utcnow
from models import WebhookEvent
from services.webhooks.decisioning import SilenceSnapshot, match_active_silence

# Hard scan bound for one backtest call; newest-first keeps counts and samples
# focused on current behavior when the cap kicks in.
_MAX_BACKTEST_SCAN = 50_000
_SCAN_BATCH_SIZE = 1_000


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

    # The matching logic is Python-side (it is the pipeline's own matcher), so
    # rows must be shipped to the worker. Keep that bounded: project only the
    # columns the matcher and the sample need (no raw_payload/ai_analysis
    # blobs), stream in batches instead of hydrating the whole window, and cap
    # the scan at the newest _MAX_BACKTEST_SCAN events. Truncation is reported
    # via ``scan_truncated`` rather than silently understating the counts.
    stmt = (
        select(
            WebhookEvent.id,
            WebhookEvent.timestamp,
            WebhookEvent.source,
            WebhookEvent.importance,
            WebhookEvent.is_duplicate,
            WebhookEvent.parsed_data,
        )
        .where(WebhookEvent.timestamp >= start_time)
        .order_by(WebhookEvent.timestamp.desc())
        .limit(_MAX_BACKTEST_SCAN)
        .execution_options(yield_per=_SCAN_BATCH_SIZE)
    )
    result = await session.stream(stmt)

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

    total_scanned = 0
    total_matched = 0
    importance_counts: dict[str, int] = {"high": 0, "medium": 0, "low": 0, "unknown": 0}
    source_counts: dict[str, int] = {}
    sample_matched_events: list[dict[str, Any]] = []

    async for event in result:
        total_scanned += 1
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
        "scan_truncated": total_scanned >= _MAX_BACKTEST_SCAN,
    }

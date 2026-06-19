"""Read-side queries for the decision trace (why an alert was forwarded/skipped).

Two shapes back the dashboard's Decision Trace tab:
- ``get_decision_trace_stats``: a time-windowed aggregate (forwarded vs skipped,
  plus the skip-reason distribution) computed with index-backed GROUP BY on the
  ``outcome`` / ``skip_code`` columns — the cheap "how many were silenced /
  cooled-down / forwarded" view.
- ``list_decision_traces``: a filterable, cursor-paginated list where each row
  carries its full ordered ``steps`` chain inline, so expanding a row needs no
  extra request.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utc_isoformat, utcnow
from db.session import count_with_timeout
from models import DecisionTrace
from services.pagination import apply_cursor_window, trim_cursor_window

_PERIOD_DELTAS = {
    "day": timedelta(days=1),
    "week": timedelta(days=7),
    "month": timedelta(days=30),
    "year": timedelta(days=365),
}


async def get_decision_trace_stats(session: AsyncSession, period: str = "day") -> dict[str, Any]:
    """Aggregate forward/skip outcomes over a time window via GROUP BY.

    ``outcome_breakdown`` answers "how many forwarded vs skipped"; ``skip_code_breakdown``
    answers "of the skips, why" (cooldown / silenced / noise_suppressed / ...).
    Both are index-backed counts over ``created_at`` >= window start.
    """
    start_time = utcnow() - _PERIOD_DELTAS.get(period, _PERIOD_DELTAS["day"])

    total_stmt = select(func.count(DecisionTrace.id)).where(DecisionTrace.created_at >= start_time)
    total = await count_with_timeout(session, total_stmt) or 0

    outcome_stmt = (
        select(DecisionTrace.outcome, func.count(DecisionTrace.id))
        .where(DecisionTrace.created_at >= start_time)
        .group_by(DecisionTrace.outcome)
    )
    outcome_rows = (await session.execute(outcome_stmt)).all()
    outcome_breakdown = {row[0]: row[1] for row in outcome_rows}

    # Skip-reason distribution is over skipped traces only (forwarded traces carry
    # skip_code="none", which would otherwise dominate and mean nothing).
    skip_stmt = (
        select(DecisionTrace.skip_code, func.count(DecisionTrace.id))
        .where(DecisionTrace.created_at >= start_time, DecisionTrace.outcome == "skipped")
        .group_by(DecisionTrace.skip_code)
        .order_by(func.count(DecisionTrace.id).desc())
    )
    skip_rows = (await session.execute(skip_stmt)).all()
    skip_code_breakdown = {row[0]: row[1] for row in skip_rows}

    return {
        "period": period,
        "total": total,
        "forwarded": outcome_breakdown.get("forwarded", 0),
        "skipped": outcome_breakdown.get("skipped", 0),
        "outcome_breakdown": outcome_breakdown,
        "skip_code_breakdown": skip_code_breakdown,
    }


def _row_to_trace_dict(trace: DecisionTrace) -> dict[str, Any]:
    return {
        "id": trace.id,
        "webhook_event_id": trace.webhook_event_id,
        "created_at": utc_isoformat(trace.created_at) if trace.created_at is not None else None,
        "outcome": trace.outcome,
        "skip_code": trace.skip_code,
        "source": trace.source,
        "importance": trace.importance,
        "is_periodic_reminder": trace.is_periodic_reminder,
        "matched_rules": trace.matched_rules or [],
        "steps": trace.steps or [],
    }


async def list_decision_traces(
    session: AsyncSession,
    *,
    cursor: int | None = None,
    outcome: str = "",
    skip_code: str = "",
    source: str = "",
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[dict[str, Any]], bool, int | None]:
    """Cursor-paginated trace list, newest first, with the full chain inline.

    A trace row is small (no large blobs), so the ordered ``steps`` chain and
    ``matched_rules`` are projected inline — expanding a row in the UI needs no
    follow-up request.
    """
    query = select(DecisionTrace)
    if outcome:
        query = query.where(DecisionTrace.outcome == outcome)
    if skip_code:
        query = query.where(DecisionTrace.skip_code == skip_code)
    if source:
        query = query.where(DecisionTrace.source == source)
    query = query.order_by(DecisionTrace.id.desc())
    query = apply_cursor_window(query, DecisionTrace.id, page=page, page_size=page_size, cursor=cursor)

    result = await session.execute(query)
    page_window = trim_cursor_window(list(result.scalars().all()), page_size, lambda trace: trace.id)
    items = [_row_to_trace_dict(trace) for trace in page_window.rows]
    return items, page_window.has_more, page_window.next_cursor


async def get_decision_trace_for_event(session: AsyncSession, webhook_event_id: int) -> dict[str, Any] | None:
    """Return the latest trace for a single webhook event, or None.

    Normally an event has exactly one trace; guard against duplicates by taking
    the newest so the per-alert "why" view is deterministic.
    """
    stmt = (
        select(DecisionTrace)
        .where(DecisionTrace.webhook_event_id == webhook_event_id)
        .order_by(DecisionTrace.id.desc())
        .limit(1)
    )
    trace = (await session.execute(stmt)).scalar_one_or_none()
    return _row_to_trace_dict(trace) if trace is not None else None

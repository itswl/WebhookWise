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

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utc_isoformat, utcnow
from db.session import count_with_timeout
from models import DecisionTrace, ForwardOutbox
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


# Analysis routes that represent a fresh LLM judgment vs. a reuse/skip/degradation.
# Accuracy-ish signals (importance distribution, override rate) are only meaningful
# over fresh "ai" judgments; everything else reuses or bypasses the model.
_AI_ROUTE = "ai"


async def get_decision_trace_quality_stats(session: AsyncSession, period: str = "day") -> dict[str, Any]:
    """Aggregate proxy signals for AI-judgment quality over a time window.

    WebhookWise has no human ground-truth label ("was this right?"), so this is
    deliberately *proxy* metrics, not a true accuracy score:
    - route_breakdown: how judgments were produced (ai vs cache/reuse/rule/...).
    - override_rate: of fresh ``ai`` judgments, how often a deterministic rule
      had to correct the AI's importance (the system disagreeing with the AI).
    - degraded / degraded_reasons: how often analysis fell back to rules, and why.
    - ai_importance_breakdown: importance distribution of fresh ``ai`` judgments
      (overall and per source) — surfaces "what is the AI tending to call this".
    """
    start_time = utcnow() - _PERIOD_DELTAS.get(period, _PERIOD_DELTAS["day"])
    window = DecisionTrace.created_at >= start_time

    total = await count_with_timeout(session, select(func.count(DecisionTrace.id)).where(window)) or 0

    route_rows = (
        await session.execute(
            select(DecisionTrace.route, func.count(DecisionTrace.id))
            .where(window)
            .group_by(DecisionTrace.route)
        )
    ).all()
    route_breakdown = {(row[0] or "unknown"): row[1] for row in route_rows}
    ai_total = route_breakdown.get(_AI_ROUTE, 0)

    # Override rate is measured only over fresh AI judgments (a reuse/rule row
    # carries a propagated or rule-derived importance, not a fresh AI call).
    ai_overrides = (
        await count_with_timeout(
            session,
            select(func.count(DecisionTrace.id)).where(
                window, DecisionTrace.route == _AI_ROUTE, DecisionTrace.importance_override.is_(True)
            ),
        )
        or 0
    )

    degraded_rows = (
        await session.execute(
            select(DecisionTrace.degraded_reason, func.count(DecisionTrace.id))
            .where(window, DecisionTrace.degraded_reason.isnot(None))
            .group_by(DecisionTrace.degraded_reason)
            .order_by(func.count(DecisionTrace.id).desc())
        )
    ).all()
    degraded_reasons = {row[0]: row[1] for row in degraded_rows}
    degraded_total = sum(degraded_reasons.values())

    # Importance distribution overall and per source, over fresh AI judgments only.
    importance_rows = (
        await session.execute(
            select(DecisionTrace.importance, func.count(DecisionTrace.id))
            .where(window, DecisionTrace.route == _AI_ROUTE, DecisionTrace.importance.isnot(None))
            .group_by(DecisionTrace.importance)
        )
    ).all()
    ai_importance_breakdown = {row[0]: row[1] for row in importance_rows}

    by_source_rows = (
        await session.execute(
            select(DecisionTrace.source, DecisionTrace.importance, func.count(DecisionTrace.id))
            .where(window, DecisionTrace.route == _AI_ROUTE, DecisionTrace.source.isnot(None))
            .group_by(DecisionTrace.source, DecisionTrace.importance)
        )
    ).all()
    ai_importance_by_source: dict[str, dict[str, int]] = {}
    for source, importance, count in by_source_rows:
        ai_importance_by_source.setdefault(source, {})[importance or "unknown"] = count

    return {
        "period": period,
        "total": total,
        "ai_total": ai_total,
        "route_breakdown": route_breakdown,
        "override_count": ai_overrides,
        "override_rate": round(ai_overrides / ai_total * 100, 1) if ai_total else 0.0,
        "degraded_total": degraded_total,
        "degraded_rate": round(degraded_total / total * 100, 1) if total else 0.0,
        "degraded_reasons": degraded_reasons,
        "ai_importance_breakdown": ai_importance_breakdown,
        "ai_importance_by_source": ai_importance_by_source,
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
        "route": trace.route,
        "importance_override": trace.importance_override,
        "degraded_reason": trace.degraded_reason,
        "matched_rules": trace.matched_rules or [],
        "steps": trace.steps or [],
    }


# Collapse an event's (possibly multiple, one-per-target) outbox rows into a
# single delivery state for the trace row. Precedence reflects "what does the
# operator most need to see": an outright failure outranks an in-flight retry,
# which outranks a clean success.
_DELIVERY_PENDING = {"pending", "processing", "retrying"}
_DELIVERY_FAILED = {"exhausted", "expired"}


def _delivery_state(statuses: list[str]) -> str:
    if any(s in _DELIVERY_FAILED for s in statuses):
        return "failed"
    if any(s in _DELIVERY_PENDING for s in statuses):
        return "pending"
    if any(s == "sent" for s in statuses):
        return "sent"
    return "unknown"


async def _attach_delivery_status(session: AsyncSession, items: list[dict[str, Any]]) -> None:
    """Annotate forwarded trace rows with their outbox delivery status, in place.

    The decision trace only records the forward *decision* (queued); whether the
    notification actually reached the target lives in ForwardOutbox. For the
    forwarded rows on this page, batch-load their outbox rows (keyed by
    webhook_event_id or original_event_id — periodic reminders forward against
    the chain head) and attach a compact ``delivery`` summary. Rows with no
    outbox match (e.g. pre-feature data) get no ``delivery`` key.
    """
    event_ids = [int(item["webhook_event_id"]) for item in items if item.get("outcome") == "forwarded"]
    if not event_ids:
        return
    id_set = set(event_ids)

    stmt = select(
        ForwardOutbox.webhook_event_id,
        ForwardOutbox.original_event_id,
        ForwardOutbox.status,
        ForwardOutbox.target_name,
        ForwardOutbox.target_type,
        ForwardOutbox.attempts,
        ForwardOutbox.last_error,
        ForwardOutbox.sent_at,
    ).where(
        or_(ForwardOutbox.webhook_event_id.in_(event_ids), ForwardOutbox.original_event_id.in_(event_ids))
    )
    rows = (await session.execute(stmt)).all()

    by_event: dict[int, list[Any]] = {}
    for row in rows:
        for eid in (row.webhook_event_id, row.original_event_id):
            if eid in id_set:
                by_event.setdefault(int(eid), []).append(row)

    for item in items:
        if item.get("outcome") != "forwarded":
            continue
        targets = by_event.get(int(item["webhook_event_id"]))
        if not targets:
            continue
        state = _delivery_state([str(t.status) for t in targets])
        # Surface the most actionable target detail: a failed one if present.
        focus = next((t for t in targets if str(t.status) in _DELIVERY_FAILED), None) or targets[0]
        item["delivery"] = {
            "state": state,
            "target_count": len(targets),
            "target_name": focus.target_name or focus.target_type or None,
            "attempts": focus.attempts,
            "last_error": focus.last_error if state == "failed" else None,
            "sent_at": utc_isoformat(focus.sent_at) if focus.sent_at is not None else None,
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
    follow-up request. Forwarded rows are also annotated with their outbox
    delivery status so the page answers "was it actually delivered" too.
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
    await _attach_delivery_status(session, items)
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

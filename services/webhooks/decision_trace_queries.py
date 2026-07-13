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

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utc_isoformat, utcnow
from core.sensitive_data import mask_webhook_url
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

    outcome_stmt = (
        select(DecisionTrace.outcome, func.count(DecisionTrace.id))
        .where(DecisionTrace.created_at >= start_time)
        .group_by(DecisionTrace.outcome)
    )
    outcome_rows = (await session.execute(outcome_stmt)).all()
    outcome_breakdown = {row[0]: row[1] for row in outcome_rows}
    # The window total is the sum of the outcome buckets — no separate COUNT query.
    total = sum(outcome_breakdown.values())

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

    route_rows = (
        await session.execute(
            select(DecisionTrace.route, func.count(DecisionTrace.id)).where(window).group_by(DecisionTrace.route)
        )
    ).all()
    route_breakdown = {(row[0] or "unknown"): row[1] for row in route_rows}
    # The window total is the sum of the route buckets — no separate COUNT query.
    total = sum(route_breakdown.values())
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


async def get_overview_stats(session: AsyncSession, period: str = "day") -> dict[str, Any]:
    """One-screen operational summary for the Overview home page.

    Composes (over the window): processed/forwarded/skipped + forward rate, the
    skip-reason distribution, top sources by volume, and a delivery success rate
    (sent vs failed outbox rows for forwarded events). All index-backed GROUP BYs
    over decision_trace + forward_outboxes; reuses the same window presets.
    """
    start_time = utcnow() - _PERIOD_DELTAS.get(period, _PERIOD_DELTAS["day"])
    window = DecisionTrace.created_at >= start_time

    outcome_rows = (
        await session.execute(
            select(DecisionTrace.outcome, func.count(DecisionTrace.id)).where(window).group_by(DecisionTrace.outcome)
        )
    ).all()
    outcome_breakdown = {row[0]: row[1] for row in outcome_rows}
    total = sum(outcome_breakdown.values())
    forwarded = outcome_breakdown.get("forwarded", 0)
    skipped = outcome_breakdown.get("skipped", 0)

    skip_rows = (
        await session.execute(
            select(DecisionTrace.skip_code, func.count(DecisionTrace.id))
            .where(window, DecisionTrace.outcome == "skipped")
            .group_by(DecisionTrace.skip_code)
            .order_by(func.count(DecisionTrace.id).desc())
        )
    ).all()
    skip_code_breakdown = {row[0]: row[1] for row in skip_rows}

    source_rows = (
        await session.execute(
            select(DecisionTrace.source, func.count(DecisionTrace.id))
            .where(window, DecisionTrace.source.isnot(None))
            .group_by(DecisionTrace.source)
            .order_by(func.count(DecisionTrace.id).desc())
            .limit(8)
        )
    ).all()
    top_sources = [{"source": row[0], "count": row[1]} for row in source_rows]

    # Delivery success rate: over outbox rows for forwarded events in the window,
    # how many reached the target (sent) vs failed (exhausted/expired). Joined on
    # created_at >= window directly on the outbox row (its own timestamp).
    delivery_rows = (
        await session.execute(
            select(ForwardOutbox.status, func.count(ForwardOutbox.id))
            .where(ForwardOutbox.created_at >= start_time)
            .group_by(ForwardOutbox.status)
        )
    ).all()
    delivery_breakdown = {row[0]: row[1] for row in delivery_rows}
    delivered = delivery_breakdown.get("sent", 0)
    failed = sum(delivery_breakdown.get(s, 0) for s in _DELIVERY_FAILED)
    delivery_total = sum(delivery_breakdown.values())

    # Previous-period total for the growth indicator (overview card).
    prev_start = start_time - _PERIOD_DELTAS.get(period, _PERIOD_DELTAS["day"])
    prev_total = (
        await count_with_timeout(
            session,
            select(func.count(DecisionTrace.id)).where(
                DecisionTrace.created_at >= prev_start, DecisionTrace.created_at < start_time
            ),
        )
        or 0
    )
    prev_forwarded_row = await session.execute(
        select(func.count(DecisionTrace.id)).where(
            DecisionTrace.created_at >= prev_start,
            DecisionTrace.created_at < start_time,
            DecisionTrace.outcome == "forwarded",
        )
    )
    prev_forwarded = prev_forwarded_row.scalar() or 0
    prev_total_val = int(prev_total or 0)
    prev_forwarded_val = int(prev_forwarded or 0)

    return {
        "period": period,
        "total": total,
        "forwarded": forwarded,
        "skipped": skipped,
        "forward_rate": round(forwarded / total * 100, 1) if total else 0.0,
        "skip_code_breakdown": skip_code_breakdown,
        "top_sources": top_sources,
        "delivery": {
            "total": delivery_total,
            "delivered": delivered,
            "failed": failed,
            "success_rate": round(delivered / delivery_total * 100, 1) if delivery_total else 0.0,
        },
        "previous": {
            "total": prev_total_val,
            "forwarded": prev_forwarded_val,
            "total_delta_pct": round((total - prev_total_val) / prev_total_val * 100, 1) if prev_total_val else None,
            "forwarded_delta_pct": round((forwarded - prev_forwarded_val) / prev_forwarded_val * 100, 1)
            if prev_forwarded_val
            else None,
        },
    }


async def get_silence_suppression_counts(
    session: AsyncSession, *, silence_ids: list[int] | None = None
) -> dict[int, dict[str, Any]]:
    """How many alerts each silence rule has suppressed (lifetime) + recency.

    Powers the Silence ROI panel: aggregates silenced decision-trace rows by the
    flattened ``silence_id`` column (set only when an alert was silenced, so the
    partial index covers exactly these rows) into
    ``{silence_id: {"count": int, "last_suppressed_at": iso|None}}``.

    A high count means the rule is pulling its weight; a *zero* count on an
    active rule is a "zombie" silence worth reviewing, and a stale
    ``last_suppressed_at`` flags one that has gone quiet. ``silence_ids`` scopes
    the GROUP BY to the rules currently shown (skips counts for since-deleted
    silences, whose trace rows still carry the old id).
    """
    stmt = (
        select(
            DecisionTrace.silence_id,
            func.count(DecisionTrace.id),
            func.max(DecisionTrace.created_at),
        )
        .where(DecisionTrace.silence_id.isnot(None))
        .group_by(DecisionTrace.silence_id)
    )
    if silence_ids:
        stmt = stmt.where(DecisionTrace.silence_id.in_(silence_ids))
    rows = (await session.execute(stmt)).all()
    return {
        int(row[0]): {
            "count": row[1],
            "last_suppressed_at": utc_isoformat(row[2]) if row[2] is not None else None,
        }
        for row in rows
    }


async def get_forward_rule_hit_counts(
    session: AsyncSession, *, rule_names: list[str] | None = None
) -> dict[str, dict[str, Any]]:
    """How many alerts each forward rule has matched (lifetime) + recency.

    The symmetric counterpart to ``get_silence_suppression_counts`` for the
    Forward-rule ROI panel: answers "which rule is carrying the load, which is a
    zombie". ``matched_rules`` is a JSONB array of rule *names* (a forwarded
    alert can match several rules when ``stop_on_match`` is false), so this
    aggregates over the forwarded traces that carry a non-empty array.

    Returns ``{rule_name: {"count": int, "last_matched_at": iso|None}}``. A zero
    count on an *enabled* rule is a zombie rule worth reviewing; a stale
    ``last_matched_at`` flags one that has gone quiet.

    Aggregation is done in Python rather than via ``jsonb_array_elements`` so it
    behaves identically on Postgres (prod) and SQLite (tests) — no dialect
    branch. Bounded by the forwarded-trace count (only forwarded rows carry a
    non-empty ``matched_rules``); we select just the two needed columns.
    """
    stmt = select(DecisionTrace.matched_rules, DecisionTrace.created_at).where(
        DecisionTrace.outcome == "forwarded",
        DecisionTrace.matched_rules.isnot(None),
    )
    rows = (await session.execute(stmt)).all()

    wanted = set(rule_names) if rule_names else None
    counts: dict[str, int] = {}
    last_at: dict[str, datetime] = {}
    for matched, created_at in rows:
        if not matched:
            continue
        for name in matched:
            if wanted is not None and name not in wanted:
                continue
            counts[name] = counts.get(name, 0) + 1
            if created_at is not None and (name not in last_at or created_at > last_at[name]):
                last_at[name] = created_at

    return {
        name: {
            "count": count,
            "last_matched_at": utc_isoformat(last_at[name]) if name in last_at else None,
        }
        for name, count in counts.items()
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
    notification actually reached the target lives in ForwardOutbox. Each trace
    row is ONE alert occurrence, and that occurrence's forward creates an outbox
    row keyed by ``webhook_event_id`` == that occurrence's id — so we match on
    that alone. We deliberately do NOT also match ``original_event_id``: in a
    dedup chain every duplicate carries the chain head as its original, so
    matching it would make the head's row absorb every later duplicate's
    delivery and falsely show N "deliveries" for one occurrence. Rows with no
    outbox match (e.g. pre-feature data) get no ``delivery`` key.
    """
    event_ids = [int(item["webhook_event_id"]) for item in items if item.get("outcome") == "forwarded"]
    if not event_ids:
        return

    stmt = select(ForwardOutbox).where(ForwardOutbox.webhook_event_id.in_(event_ids))
    rows = list((await session.execute(stmt)).scalars().all())

    by_event: dict[int, list[ForwardOutbox]] = {}
    for row in rows:
        if row.webhook_event_id is not None:
            by_event.setdefault(int(row.webhook_event_id), []).append(row)

    for item in items:
        if item.get("outcome") != "forwarded":
            continue
        targets = by_event.get(int(item["webhook_event_id"]))
        if not targets:
            continue
        targets.sort(key=lambda t: t.id)
        state = _delivery_state([str(t.status) for t in targets])
        focus = next((t for t in targets if str(t.status) in _DELIVERY_FAILED), None) or targets[0]
        item["delivery"] = {
            "state": state,
            "target_count": len(targets),
            # Collapsed summary (badge + at-a-glance), kept for the row header.
            "target_name": focus.target_name or focus.target_type or None,
            "attempts": focus.attempts,
            "last_error": focus.last_error if state == "failed" else None,
            "sent_at": utc_isoformat(focus.sent_at) if focus.sent_at is not None else None,
            # Full per-target detail for the expanded view (mirrors the outbox tab).
            "targets": [_outbox_target_dict(t) for t in targets],
        }


def _outbox_target_dict(row: ForwardOutbox) -> dict[str, Any]:
    return {
        "outbox_id": row.id,
        "target_type": row.target_type,
        "target_name": row.target_name or None,
        # Masked: the raw URL embeds the bot-hook secret token; never ship it to the browser.
        "target_url": mask_webhook_url(row.target_url) or None,
        "rule_name": row.rule_name or None,
        "event_type": row.event_type or None,
        "is_periodic_reminder": row.is_periodic_reminder,
        "status": row.status,
        "attempts": row.attempts,
        "max_attempts": row.max_attempts,
        "last_error": row.last_error,
        "sent_at": utc_isoformat(row.sent_at) if row.sent_at is not None else None,
        "next_attempt_at": utc_isoformat(row.next_attempt_at) if row.next_attempt_at is not None else None,
        "created_at": utc_isoformat(row.created_at) if row.created_at is not None else None,
        # Whether this target can be manually re-enqueued (same rule as the outbox tab).
        "retryable": row.status in ("exhausted", "expired", "retrying"),
    }


async def list_decision_traces(
    session: AsyncSession,
    *,
    cursor: int | None = None,
    outcome: str = "",
    skip_code: str = "",
    source: str = "",
    delivery: str = "",
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
    if delivery == "failed":
        # "Delivery failed" = the alert was forwarded but at least one of its
        # outbox targets is exhausted/expired. Filter at the SQL level (EXISTS)
        # so cursor pagination stays correct, rather than post-filtering a page.
        failed_outbox = (
            select(ForwardOutbox.id)
            .where(
                ForwardOutbox.webhook_event_id == DecisionTrace.webhook_event_id,
                ForwardOutbox.status.in_(tuple(_DELIVERY_FAILED)),
            )
            .exists()
        )
        query = query.where(DecisionTrace.outcome == "forwarded", failed_outbox)
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
    if trace is None:
        return None
    item = _row_to_trace_dict(trace)
    # Attach delivery status too, so the per-alert view answers "delivered?" like
    # the list does (no-op for skipped rows / rows without an outbox record).
    await _attach_delivery_status(session, [item])
    return item

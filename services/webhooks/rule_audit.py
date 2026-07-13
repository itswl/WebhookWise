"""Alert-rule audit: surface zombie rules, pure-noise rules, and flapping rules.

Reads already-collected webhook_events + decision_trace tables; no new
instruments, no hot-path impact. Grouped by (source, rule_name) — the
same granularity the periodic report already uses.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import Integer, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utcnow
from models import DecisionTrace, WebhookEvent

_RULE_NAME_EXPR = func.coalesce(
    WebhookEvent.parsed_data["RuleName"].astext,
    WebhookEvent.parsed_data["AlertName"].astext,
    WebhookEvent.parsed_data["MetricName"].astext,
    WebhookEvent.parsed_data["Type"].astext,
)


async def get_rule_audit(
    session: AsyncSession,
    *,
    window_days: int = 30,
    min_events: int = 3,
    include_forward_counts: bool = True,
) -> list[dict[str, Any]]:
    """Aggregate alert-rule health over *window_days*.

    Each row represents one (source, rule_name) pair. ``flags`` is a list of
    human-readable tags:
    - **zombie**: has a ``last_seen`` older than half the window (gone quiet).
    - **pure_noise**: every event was a duplicate or was skipped (silenced /
      noise_suppressed). The rule fires, but never forwards — possibly a
      misconfigured threshold or a candidate for a silence rule.
    - **flapping**: fired in more than 70% of the intervals within the window
      (bin day-resolution). Suggests the threshold is set right at the noise
      floor — the alert oscillates between firing/resolving on its own.
    """
    window_days = max(1, int(window_days))
    min_events = max(1, int(min_events))
    start = utcnow() - timedelta(days=window_days)

    rows = (
        await session.execute(
            select(
                WebhookEvent.source,
                _RULE_NAME_EXPR.label("rule_name"),
                func.count(WebhookEvent.id).label("total"),
                func.sum(WebhookEvent.is_duplicate.cast(Integer)).label("duplicates"),
                func.max(WebhookEvent.timestamp).label("last_seen"),
                func.min(WebhookEvent.timestamp).label("first_seen"),
            )
            .where(WebhookEvent.timestamp >= start)
            .group_by(WebhookEvent.source, _RULE_NAME_EXPR)
            .having(func.count(WebhookEvent.id) >= min_events)
            .order_by(func.count(WebhookEvent.id).desc())
        )
    ).all()

    if not rows:
        return []

    # Batch-resolve forward outcomes for ALL rules in one query. Each forwarded
    # trace row carries a matched_rules JSONB array of rule names; we count how
    # often each rule name appears in any forwarded outcome (outcome='forwarded').
    # This is a read-only aggregate over the same window, kept outside the main
    # GROUP BY so it adds no round-trips per rule.
    rule_names = sorted({(r[0] or "").strip() for r in rows if r[0]})
    forwarded_by_rule: dict[str, int] = {}
    skipped_by_rule: dict[str, int] = {}
    if rule_names and include_forward_counts:
        traces = await _trace_forward_counts(session, start, rule_names)
        forwarded_by_rule, skipped_by_rule = traces

    results: list[dict[str, Any]] = []
    for row in rows:
        source = (row[0] or "unknown").strip()
        rule_name = (row[1] or "unknown").strip()
        total = int(row[2] or 0)
        duplicates = int(row[3] or 0)
        last_seen = row[4]
        first_seen = row[5]
        forwarded = forwarded_by_rule.get(rule_name, 0)

        flags: list[str] = []
        if last_seen is not None and (utcnow() - last_seen).days >= max(1, window_days // 2):
            flags.append("zombie")
        if include_forward_counts and forwarded == 0 and total > 0:
            flags.append("pure_noise")
        if first_seen is not None and last_seen is not None and first_seen != last_seen:
            days_active = max(1, (last_seen - first_seen).days)
            # More than 70% of the window days had at least one event.
            if total >= int(days_active * 0.7):
                flags.append("flapping")

        duplicate_pct = round(100.0 * duplicates / total, 1) if total else 0.0
        results.append(
            {
                "source": source,
                "rule_name": rule_name,
                "total": total,
                "duplicates": duplicates,
                "duplicate_pct": duplicate_pct,
                "forwarded": forwarded,
                "skipped": total - forwarded,
                "last_seen": last_seen.isoformat() if last_seen is not None else None,
                "first_seen": first_seen.isoformat() if first_seen is not None else None,
                "flags": flags,
            }
        )

    return results


async def _trace_forward_counts(
    session: AsyncSession, start: Any, rule_names: list[str]
) -> tuple[dict[str, int], dict[str, int]]:
    """Count per-rule-name forward/skip totals from decision_trace in one query.

    ``matched_rules`` is a JSONB array of rule names on forwarded traces, so
    with PostgreSQL we could use ``jsonb_array_elements_text``; to keep it
    portable (SQLite test shim), we load the rows and count in Python. The
    cardinality is low (one row per forwarded trace in the window), so this is
    not a bottleneck.
    """
    stmt = select(
        DecisionTrace.outcome,
        DecisionTrace.matched_rules,
    ).where(
        DecisionTrace.created_at >= start,
        DecisionTrace.matched_rules.isnot(None),
    )
    rows = (await session.execute(stmt)).all()
    name_set = set(rule_names)
    forwarded: dict[str, int] = {}
    skipped: dict[str, int] = {}
    for outcome, matched in rows:
        if not isinstance(matched, list):
            continue
        for name in matched:
            if isinstance(name, str) and name.strip() in name_set:
                key = name.strip()
                if outcome == "forwarded":
                    forwarded[key] = forwarded.get(key, 0) + 1
                else:
                    skipped[key] = skipped.get(key, 0) + 1
    return forwarded, skipped

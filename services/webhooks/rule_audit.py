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
    # r[1] is rule_name; r[0] is source. Reading r[0] here collected SOURCE
    # names, matched them against decision_trace.matched_rules (which holds
    # RULE names), never hit, and left forwarded == 0 for every rule — so the
    # pure_noise flag fired on every rule with events, including ones that
    # forward everything. An off-by-one column index that inverted a whole
    # diagnostic.
    rule_names = sorted({(r[1] or "").strip() for r in rows if r[1]})
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
    """Count per-ALERT-rule forward/skip totals from decision_trace.

    Keyed on ``decision_trace.alert_name`` — the alert rule — because that is
    the dimension this audit groups by. It previously joined against
    ``matched_rules``, which holds FORWARD rule names ("所有告警通知"): a
    disjoint namespace from alert rule names ("示例充值超限告警"), so the
    join could never hit and every rule looked like it forwarded nothing.

    Grouped in SQL on an indexed column (migration 0025 added alert_name with a
    partial index), so this is one aggregate rather than a row scan.
    """
    stmt = (
        select(
            DecisionTrace.alert_name,
            DecisionTrace.outcome,
            func.count(DecisionTrace.id),
        )
        .where(
            DecisionTrace.created_at >= start,
            DecisionTrace.alert_name.isnot(None),
        )
        .group_by(DecisionTrace.alert_name, DecisionTrace.outcome)
    )
    rows = (await session.execute(stmt)).all()
    # Alert names arrive from upstream payloads with inconsistent casing
    # (Grafana sends "DatasourceNoData", the identity extractor lowercases some
    # paths), so match case-insensitively against the audited names.
    by_lower = {name.lower(): name for name in rule_names}
    forwarded: dict[str, int] = {}
    skipped: dict[str, int] = {}
    for alert_name, outcome, count in rows:
        key = by_lower.get(str(alert_name or "").strip().lower())
        if key is None:
            continue
        if outcome == "forwarded":
            forwarded[key] = forwarded.get(key, 0) + int(count)
        else:
            skipped[key] = skipped.get(key, 0) + int(count)
    return forwarded, skipped

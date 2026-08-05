"""Alert-rule audit: surface zombie rules, pure-noise rules, and per-rule volume.

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


_HIGH_VOLUME_PER_DAY = 10.0


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
    Volume comes back as ``events_per_active_day`` — a number, not a verdict.
    True flapping (firing↔resolved oscillation) is detected live by
    services/webhooks/flapping.py and surfaced in the Action Center; this
    read-only audit deliberately does not guess at it.
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
        # No "flapping" flag here on purpose. This module used to derive one
        # from `total >= days_active * 0.7` — i.e. any rule averaging more than
        # 0.7 events per active day — which flagged EVERY rule in a real
        # estate (all 8 in production, business alerts included) and so carried
        # zero signal. Real flapping is firing↔resolved OSCILLATION, which
        # services/webhooks/flapping.py already detects properly (Redis state
        # machine, FLAPPING_MIN_TRANSITIONS) and the Action Center surfaces
        # live. Reproducing it historically would need a per-event recovery
        # marker that is not persisted; a second-rate proxy next to a working
        # detector is worse than no flag. `events_per_active_day` below gives
        # the volume this heuristic was gesturing at, as a number to judge
        # rather than a verdict to trust.

        duplicate_pct = round(100.0 * duplicates / total, 1) if total else 0.0
        days_active = max(1, (last_seen - first_seen).days) if first_seen is not None and last_seen is not None else 1
        events_per_active_day = round(total / days_active, 2)
        # A stated threshold, not a hidden one: ten a day is where a single
        # rule starts dominating an operator's notification stream. This is a
        # VOLUME observation ("this one is loud"), never a diagnosis of why.
        if events_per_active_day >= _HIGH_VOLUME_PER_DAY:
            flags.append("high_volume")
        results.append(
            {
                "source": source,
                "rule_name": rule_name,
                "total": total,
                "duplicates": duplicates,
                "duplicate_pct": duplicate_pct,
                "events_per_active_day": events_per_active_day,
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
    disjoint namespace from alert rule names ("充值金额单次超500报警"), so the
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

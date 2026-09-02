"""Noise-reduction analytics, recommendations, and reversible actions."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import case, distinct, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utc_isoformat, utcnow
from core.text import split_csv_lower
from models import DecisionTrace, ForwardRule, InboundRule, NoiseReductionAction, Silence, WebhookEvent
from services.forwarding.outbox_records import digest_window_start
from services.forwarding.rules import update_forward_rule
from services.incidents.grouping import is_recovery_payload
from services.operations.audit_logger import add_audit
from services.silences.store import create_silence, lift_silence, list_silences
from services.webhooks.decisioning import csv_value_matches
from services.webhooks.inbound_rules import (
    create_inbound_rule,
    publish_inbound_rules_invalidation,
    update_inbound_rule,
)
from services.webhooks.inbound_rules import validate as validate_inbound_rule
from services.webhooks.policies import is_synthetic_source, synthetic_sources
from services.webhooks.rule_audit import get_rule_audit
from services.webhooks.types import DIGEST, DIGEST_WINDOW_MINUTES_DEFAULT

_NOISE_SKIP_CODES = frozenset({"cooldown", "duplicate_no_rule", "noise_suppressed", "silenced"})
_MINUTES_PER_AVOIDED_NOTIFICATION = 3
_MAX_SOURCES = 12
_MAX_RECOVERY_SAMPLE = 20_000
# A rule has to fire this often, with this share of repeats, before batching it
# is worth proposing. The count is an operator policy (NOISE_DIGEST_MIN_ALERTS);
# the share is not, because below it a digest has almost nothing to batch.
_DIGEST_MIN_REPEAT_RATE = 40.0

# Event types that are WebhookWise reporting on ITSELF — an incident opening, an
# SLA breaching, a forward giving up. `outbox_exhausted` is the name this system
# emits; `forward_exhausted` is accepted as the same idea under an older name.
_SYSTEM_EVENT_TYPES = frozenset(
    {
        "incident_created",
        "incident_resolved",
        "sla_breached",
        "deep_analysis",
        "ai_error",
        "ai_degraded",
        "outbox_exhausted",
        "forward_exhausted",
    }
)
# Targets that exist to COMPARE this system against another one rather than to
# tell a person something.
_COMPARISON_TARGET_TYPES = frozenset({"feishu_relay", "relay"})
_COMPARISON_NAME_PREFIX = "shadow:"


def _is_tunable_forward_rule(rule: ForwardRule) -> bool:
    """Whether noise reduction may propose a change to this forward rule.

    Two kinds of rule are off limits, and the reason is the same for both: the
    number the suggestion is built from does not mean what it appears to mean.

    A rule matching only system event types (`incident_created,incident_resolved`)
    never carries an alert, so its "duplicate rate" is a statement about system
    cards; switching it to new-alerts-only would silently drop incident
    notifications. A shadow/relay comparison rule exists so that a second
    implementation sees the SAME traffic — tuning it destroys the comparison it
    was built for, and nothing about it is a delivery a person reads.

    Measured on production 2026-09-02: the noise centre proposed three identical
    "notify on new alerts only" changes, one of them against the shadow relay
    rule and one against an incident-notification rule.
    """
    target_type = str(rule.target_type or "").strip().lower()
    if target_type in _COMPARISON_TARGET_TYPES:
        return False
    if str(rule.name or "").strip().lower().startswith(_COMPARISON_NAME_PREFIX):
        return False
    event_types = split_csv_lower(str(rule.match_event_type or ""))
    # An empty criterion matches everything, including alerts; only a rule whose
    # every named type is a system event is excluded.
    return not (event_types and all(event_type in _SYSTEM_EVENT_TYPES for event_type in event_types))


def _pct(value: int, total: int) -> float:
    return round(value / total * 100, 1) if total else 0.0


def _suggestion_id(kind: str, *parts: object) -> str:
    identity = "|".join([kind, *(str(part).strip().lower() for part in parts)])
    return f"{kind}:{hashlib.sha256(identity.encode()).hexdigest()[:16]}"


def _rule_identity(parsed_data: Mapping[str, object] | None) -> tuple[str, str] | None:
    if not isinstance(parsed_data, dict):
        return None
    for key in ("RuleName", "AlertName", "MetricName", "Type"):
        value = parsed_data.get(key)
        if value is not None and not isinstance(value, dict | list):
            text = str(value).strip()
            if text:
                return key, text[:200]
    return None


async def _window_metrics(
    session: AsyncSession,
    *,
    start: datetime,
    end: datetime,
    include_sources: bool,
) -> dict[str, Any]:
    event_window = (WebhookEvent.timestamp >= start) & (WebhookEvent.timestamp < end)
    trace_window = (DecisionTrace.created_at >= start) & (DecisionTrace.created_at < end)
    # A probe fires on a timer and never stops; it is not noise anybody can
    # tune, and leaving it in makes every rate on this page read wrong. Applied
    # to both windows so the tables still sum to the summary above them.
    synthetic = sorted(synthetic_sources())
    if synthetic:
        event_window = event_window & func.lower(func.coalesce(WebhookEvent.source, "")).notin_(synthetic)
        trace_window = trace_window & func.lower(func.coalesce(DecisionTrace.source, "")).notin_(synthetic)

    event_row = (
        await session.execute(
            select(
                func.count(WebhookEvent.id),
                func.sum(case((WebhookEvent.is_duplicate.is_(True), 1), else_=0)),
            ).where(event_window)
        )
    ).one()
    total = int(event_row[0] or 0)
    duplicates = int(event_row[1] or 0)

    trace_rows = (
        await session.execute(
            select(
                DecisionTrace.outcome,
                DecisionTrace.skip_code,
                func.count(DecisionTrace.id),
            )
            .where(trace_window)
            .group_by(DecisionTrace.outcome, DecisionTrace.skip_code)
        )
    ).all()
    forwarded = 0
    filtered = 0
    skip_breakdown: dict[str, int] = {}
    for outcome, skip_code, count in trace_rows:
        count_value = int(count or 0)
        if outcome == "forwarded":
            forwarded += count_value
        if outcome == "skipped":
            key = str(skip_code or "unknown")
            skip_breakdown[key] = skip_breakdown.get(key, 0) + count_value
            if key in _NOISE_SKIP_CODES:
                filtered += count_value

    noise_condition = or_(
        WebhookEvent.is_duplicate.is_(True),
        DecisionTrace.skip_code.in_(sorted(_NOISE_SKIP_CODES)),
    )
    noise_events = int(
        (
            await session.execute(
                select(func.count(distinct(WebhookEvent.id)))
                .select_from(WebhookEvent)
                .outerjoin(DecisionTrace, DecisionTrace.webhook_event_id == WebhookEvent.id)
                .where(event_window, noise_condition)
            )
        ).scalar_one()
        or 0
    )

    result: dict[str, Any] = {
        "total": total,
        "duplicates": duplicates,
        "duplicate_rate": _pct(duplicates, total),
        "noise_events": noise_events,
        "noise_rate": _pct(noise_events, total),
        "forwarded": forwarded,
        "notifications_avoided": filtered,
        "estimated_minutes_saved": filtered * _MINUTES_PER_AVOIDED_NOTIFICATION,
        "skip_breakdown": skip_breakdown,
    }
    if not include_sources:
        # Aggregate-only view (the previous/comparison window): everything above
        # comes from index-backed GROUP BYs. The payload sample below exists to
        # power recovery detection, rule identities, and the sources table —
        # a Python-side pass over up to _MAX_RECOVERY_SAMPLE JSONB rows — so
        # the aggregate view skips it and carries no recovery_* keys.
        return result

    payload_rows = (
        await session.execute(
            select(
                WebhookEvent.source,
                WebhookEvent.parsed_data,
                WebhookEvent.ai_analysis,
            )
            .where(event_window)
            .order_by(WebhookEvent.timestamp.desc(), WebhookEvent.id.desc())
            .limit(_MAX_RECOVERY_SAMPLE)
        )
    ).all()
    recovery_sampled = total > len(payload_rows)
    recovery_denominator = len(payload_rows) if recovery_sampled else total
    recoveries = 0
    recovery_by_source: dict[str, int] = {}
    recovery_by_rule: dict[str, int] = {}
    rule_keys: dict[tuple[str, str], str] = {}
    for source, parsed_data, ai_analysis in payload_rows:
        source_name = str(source or "unknown").strip()
        identity = _rule_identity(parsed_data)
        rule_label = identity[1] if identity is not None else source_name
        if is_recovery_payload(parsed_data, ai_analysis):
            recoveries += 1
            recovery_by_source[source_name] = recovery_by_source.get(source_name, 0) + 1
            recovery_by_rule[rule_label] = recovery_by_rule.get(rule_label, 0) + 1
        if identity is not None:
            key, rule_name = identity
            rule_keys.setdefault((source_name, rule_name), key)
    result.update(
        {
            "recoveries": recoveries,
            "recovery_rate": _pct(recoveries, recovery_denominator),
            "recovery_sampled": recovery_sampled,
            "recovery_sample_size": len(payload_rows),
            "_rule_keys": rule_keys,
        }
    )

    event_source_rows = (
        await session.execute(
            select(
                WebhookEvent.source,
                func.count(WebhookEvent.id),
                func.sum(case((WebhookEvent.is_duplicate.is_(True), 1), else_=0)),
            )
            .where(event_window)
            .group_by(WebhookEvent.source)
        )
    ).all()
    trace_source_rows = (
        await session.execute(
            select(
                DecisionTrace.source,
                DecisionTrace.outcome,
                DecisionTrace.skip_code,
                func.count(DecisionTrace.id),
            )
            .where(trace_window, DecisionTrace.source.isnot(None))
            .group_by(DecisionTrace.source, DecisionTrace.outcome, DecisionTrace.skip_code)
        )
    ).all()
    noise_source_rows = (
        await session.execute(
            select(WebhookEvent.source, func.count(distinct(WebhookEvent.id)))
            .select_from(WebhookEvent)
            .outerjoin(DecisionTrace, DecisionTrace.webhook_event_id == WebhookEvent.id)
            .where(event_window, noise_condition)
            .group_by(WebhookEvent.source)
        )
    ).all()
    trace_by_source: dict[str, dict[str, int]] = {}
    for source, outcome, skip_code, count in trace_source_rows:
        source_name = str(source or "unknown").strip()
        stats = trace_by_source.setdefault(source_name, {"forwarded": 0, "filtered": 0})
        count_value = int(count or 0)
        if outcome == "forwarded":
            stats["forwarded"] += count_value
        if outcome == "skipped" and str(skip_code or "") in _NOISE_SKIP_CODES:
            stats["filtered"] += count_value
    noise_by_source = {str(source or "unknown").strip(): int(count or 0) for source, count in noise_source_rows}

    sources: list[dict[str, Any]] = []
    for source, count, duplicate_count in event_source_rows:
        source_name = str(source or "unknown").strip()
        source_total = int(count or 0)
        source_duplicates = int(duplicate_count or 0)
        trace_stats = trace_by_source.get(source_name, {"forwarded": 0, "filtered": 0})
        source_noise = noise_by_source.get(source_name, 0)
        sources.append(
            {
                "source": source_name,
                "total": source_total,
                "duplicates": source_duplicates,
                "duplicate_rate": _pct(source_duplicates, source_total),
                "noise_events": source_noise,
                "noise_rate": _pct(source_noise, source_total),
                "recoveries": recovery_by_source.get(source_name, 0),
                "forwarded": trace_stats["forwarded"],
                "notifications_avoided": trace_stats["filtered"],
            }
        )
    result["sources"] = sorted(sources, key=lambda item: (-int(item["noise_events"]), -int(item["total"])))[
        :_MAX_SOURCES
    ]

    # The UI table ranks by alert RULE — "grafana: 189" names an ecosystem,
    # not a culprit. Traces carry alert_name; events without one fall back to
    # their source so unidentified senders stay visible. The source-grain
    # list above stays: the suggestion engine matches rules by source.
    trace_name_rows = (
        await session.execute(
            select(
                DecisionTrace.webhook_event_id, DecisionTrace.alert_name, DecisionTrace.outcome, DecisionTrace.skip_code
            ).where(trace_window)
        )
    ).all()
    name_by_event: dict[int, str | None] = {}
    avoided_by_rule: dict[str, int] = {}
    # Labels that are a real alert rule name, not a source used as a fallback
    # label. Only the former can be written into an inbound rule's
    # match_rule_name, so only the former can be proposed as a digest.
    alert_rule_names: set[str] = set()
    for event_id, alert_name, _outcome, _skip_code in trace_name_rows:
        if event_id is not None:
            name_by_event[int(event_id)] = alert_name
        if alert_name:
            alert_rule_names.add(str(alert_name))
    event_rows = (
        await session.execute(
            select(
                WebhookEvent.id,
                WebhookEvent.source,
                WebhookEvent.is_duplicate,
                WebhookEvent.timestamp,
                WebhookEvent.created_at,
            ).where(event_window)
        )
    ).all()
    noise_ids = {
        int(row[0])
        for row in (
            await session.execute(
                select(distinct(WebhookEvent.id))
                .select_from(WebhookEvent)
                .outerjoin(DecisionTrace, DecisionTrace.webhook_event_id == WebhookEvent.id)
                .where(event_window, noise_condition)
            )
        ).all()
    }
    rules_agg: dict[str, dict[str, int]] = {}
    sources_by_rule: dict[str, list[str]] = {}
    # Which digest windows a rule actually fired in. A digest sends one card per
    # window it fired in, so this is what the saving is measured against; the
    # count of windows in the range would price the quiet ones too.
    windows_by_rule: dict[str, set[datetime]] = {}
    for event_id, source, is_duplicate, event_time, created_at in event_rows:
        source_label = str(source or "unknown").strip()
        label = name_by_event.get(int(event_id)) or source_label
        stats = rules_agg.setdefault(label, {"total": 0, "duplicates": 0, "noise_events": 0})
        stats["total"] += 1
        if is_duplicate:
            stats["duplicates"] += 1
        if int(event_id) in noise_ids:
            stats["noise_events"] += 1
        rule_sources = sources_by_rule.setdefault(label, [])
        if source_label not in rule_sources:
            rule_sources.append(source_label)
        fired_at = event_time or created_at
        if fired_at is not None:
            windows_by_rule.setdefault(label, set()).add(digest_window_start(fired_at, DIGEST_WINDOW_MINUTES_DEFAULT))
    for _event_id, alert_name, outcome, skip_code in trace_name_rows:
        if outcome == "skipped" and str(skip_code or "") in _NOISE_SKIP_CODES and alert_name:
            avoided_by_rule[alert_name] = avoided_by_rule.get(alert_name, 0) + 1
    noisy_rules = [
        {
            "name": label,
            "sources": sources_by_rule.get(label, []),
            "total": stats["total"],
            "duplicates": stats["duplicates"],
            "duplicate_rate": _pct(stats["duplicates"], stats["total"]),
            "noise_events": stats["noise_events"],
            "noise_rate": _pct(stats["noise_events"], stats["total"]),
            "recoveries": recovery_by_rule.get(label, 0),
            "notifications_avoided": avoided_by_rule.get(label, 0),
            "firing_windows": len(windows_by_rule.get(label, ())),
        }
        for label, stats in rules_agg.items()
    ]
    noisy_rules.sort(
        key=lambda item: (-rules_agg[str(item["name"])]["noise_events"], -rules_agg[str(item["name"])]["total"])
    )
    result["noisy_rules"] = noisy_rules[:_MAX_SOURCES]
    result["_alert_rule_names"] = alert_rule_names
    return result


def _source_for_rule(rule: ForwardRule, sources: list[dict[str, Any]], summary: dict[str, Any]) -> dict[str, Any]:
    match_source = str(rule.match_source or "").strip()
    values = [value.strip() for value in match_source.split(",") if value.strip()]
    if len(values) == 1 and not values[0].startswith("!"):
        wanted = values[0].lower()
        for source in sources:
            if str(source["source"]).lower() == wanted:
                return source
    return {
        "source": "all",
        "total": summary["total"],
        "duplicates": summary["duplicates"],
        "duplicate_rate": summary["duplicate_rate"],
        "noise_events": summary["noise_events"],
        "noise_rate": summary["noise_rate"],
    }


def _digest_min_alerts() -> int:
    """How often a rule must fire before batching it is worth proposing."""
    from core.app_context import get_config_manager
    from services.operations import runtime_settings as rt

    cfg = get_config_manager().noise
    return max(1, int(rt.override_or("NOISE_DIGEST_MIN_ALERTS", int(cfg.NOISE_DIGEST_MIN_ALERTS))))


def _digest_expected_reduction(total: int, *, firing_windows: int) -> int:
    """Cards a digest removes: one per alert today, one per window it fired in.

    Measured against the windows the rule ACTUALLY fired in, not the windows in
    the range. Pricing the quiet ones too made the estimate negative for every
    rule that fires less than once an hour — which is every candidate the
    production noise table has ever produced — and a clamp to zero would have
    shown "saves 0 cards" beside a rule that repeats two thirds of the time.
    """
    return max(0, total - max(0, firing_windows))


async def _digested_rule_names(session: AsyncSession) -> list[str]:
    """`match_rule_name` of every enabled digest rule, as the matcher sees it."""
    rows = (
        await session.execute(
            select(InboundRule.match_rule_name).where(
                InboundRule.enabled.is_(True),
                InboundRule.action == DIGEST,
            )
        )
    ).all()
    return [str(row[0] or "") for row in rows]


async def _digest_suggestions(
    session: AsyncSession,
    *,
    window_days: int,
    noisy_rules: list[dict[str, Any]],
    alert_rule_names: set[str],
) -> list[dict[str, Any]]:
    """Propose an hourly digest for an alert rule that repeats itself.

    A cap changes what a card SAYS; it does not change how many cards there
    are. Once a rule fires often enough and most of those firings are repeats,
    the operator's question is cadence, and `digest` is the verb for it.
    """
    minimum = _digest_min_alerts()
    covered = await _digested_rule_names(session)
    suggestions: list[dict[str, Any]] = []
    for row in noisy_rules:
        rule_name = str(row.get("name") or "")
        if rule_name not in alert_rule_names:
            # A source used as a fallback label, not an alert rule: an inbound
            # rule keyed on it would match nothing.
            continue
        total = int(row.get("total") or 0)
        duplicates = int(row.get("duplicates") or 0)
        duplicate_rate = float(row.get("duplicate_rate") or 0)
        if total < minimum or duplicate_rate < _DIGEST_MIN_REPEAT_RATE:
            continue
        if any(csv_value_matches(existing, rule_name) for existing in covered):
            # Already batched — including by a broad rule that names no alert
            # rule at all, which covers this one too.
            continue
        expected = _digest_expected_reduction(total, firing_windows=int(row.get("firing_windows") or 0))
        suggestions.append(
            {
                "id": _suggestion_id("digest", rule_name),
                "kind": "digest",
                "priority": "high" if total >= minimum * 2 else "medium",
                "risk": "low",
                "title": f"Deliver {rule_name} as an hourly digest",
                "reason": (
                    f"{rule_name} fired {total} times in the last {window_days} days and "
                    f"{duplicate_rate:.1f}% of those were repeats. Every alert is still stored, "
                    "judged and traced; only the chat cards are batched."
                ),
                "scope": {
                    "rule_name": rule_name,
                    "sources": list(row.get("sources") or []),
                    "window_minutes": DIGEST_WINDOW_MINUTES_DEFAULT,
                    "total": total,
                    "duplicates": duplicates,
                    "duplicate_rate": duplicate_rate,
                },
                "confidence": 0.85 if duplicate_rate >= 60 else 0.7,
                "estimated_notifications": expected,
                "estimated_minutes_saved": expected * _MINUTES_PER_AVOIDED_NOTIFICATION,
                "action_available": True,
                "reversible": True,
            }
        )
    return suggestions


async def _build_suggestions(
    session: AsyncSession,
    *,
    window_days: int,
    summary: dict[str, Any],
    sources: list[dict[str, Any]],
    noisy_rules: list[dict[str, Any]],
    rule_keys: dict[tuple[str, str], str],
    alert_rule_names: set[str],
) -> list[dict[str, Any]]:
    rules = list(
        (
            await session.execute(
                select(ForwardRule).where(ForwardRule.enabled.is_(True)).order_by(ForwardRule.priority.desc())
            )
        )
        .scalars()
        .all()
    )
    active_silences = await list_silences(session, active_only=True)
    active_signatures = {
        (str(item.match_source or "").lower(), str(item.match_payload or "").lower()) for item in active_silences
    }
    suggestions: list[dict[str, Any]] = []

    for rule in rules:
        if str(rule.match_duplicate or "all") != "all":
            continue
        if not _is_tunable_forward_rule(rule):
            continue
        stats = _source_for_rule(rule, sources, summary)
        total = int(stats.get("total") or 0)
        duplicates = int(stats.get("duplicates") or 0)
        duplicate_rate = float(stats.get("duplicate_rate") or 0)
        if total < 10 or duplicates < 5 or duplicate_rate < 35:
            continue
        exact_source = str(stats.get("source")) != "all"
        suggestions.append(
            {
                "id": _suggestion_id("duplicate_filter", rule.id),
                "kind": "duplicate_filter",
                "priority": "high" if duplicates >= 20 else "medium",
                "risk": "low",
                "title": f"Forward only new alerts through {rule.name}",
                "reason": (
                    f"{duplicate_rate:.1f}% of alerts in this rule's observed scope were duplicates "
                    f"during the last {window_days} days."
                ),
                "scope": {
                    "rule_id": int(rule.id),
                    "rule_name": rule.name,
                    "source": stats.get("source"),
                    "total": total,
                    "duplicates": duplicates,
                    "duplicate_rate": duplicate_rate,
                },
                "confidence": 0.9 if exact_source else 0.75,
                "estimated_notifications": duplicates,
                "estimated_minutes_saved": duplicates * _MINUTES_PER_AVOIDED_NOTIFICATION,
                "action_available": True,
                "reversible": True,
            }
        )

    audit_rows = await get_rule_audit(
        session,
        window_days=window_days,
        min_events=3,
        include_forward_counts=False,
    )
    for row in audit_rows:
        source = str(row.get("source") or "unknown")
        if is_synthetic_source(source):
            # get_rule_audit runs its own window query, so the exclusion applied
            # to the metrics windows above has to be repeated here.
            continue
        rule_name = str(row.get("rule_name") or "unknown")
        total = int(row.get("total") or 0)
        duplicates = int(row.get("duplicates") or 0)
        duplicate_rate = float(row.get("duplicate_pct") or 0)
        flags = set(row.get("flags") or [])
        match_key = rule_keys.get((source, rule_name))
        match_payload = f"{match_key}={rule_name}" if match_key else ""

        if (
            total >= 10
            and duplicates >= 8
            and duplicate_rate >= 90
            and match_payload
            and "," not in rule_name
            and "=" not in rule_name
            and (source.lower(), match_payload.lower()) not in active_signatures
        ):
            suggestions.append(
                {
                    "id": _suggestion_id("temporary_silence", source, match_payload),
                    "kind": "temporary_silence",
                    "priority": "high" if duplicates >= 25 else "medium",
                    "risk": "medium",
                    "title": f"Temporarily silence noisy alert {rule_name}",
                    "reason": (
                        f"{duplicates} of {total} occurrences were duplicates. A 24-hour silence "
                        "can verify the impact before making a permanent upstream change."
                    ),
                    "scope": {
                        "source": source,
                        "rule_name": rule_name,
                        "match_payload": match_payload,
                        "duration_hours": 24,
                        "total": total,
                        "duplicates": duplicates,
                        "duplicate_rate": duplicate_rate,
                    },
                    "confidence": 0.92,
                    "estimated_notifications": duplicates,
                    "estimated_minutes_saved": duplicates * _MINUTES_PER_AVOIDED_NOTIFICATION,
                    "action_available": True,
                    "reversible": True,
                }
            )

        if "flapping" in flags and total >= 7:
            suggestions.append(
                {
                    "id": _suggestion_id("tune_threshold", source, rule_name),
                    "kind": "tune_threshold",
                    "priority": "medium",
                    "risk": "external",
                    "title": f"Tune the upstream threshold for {rule_name}",
                    "reason": (
                        f"This alert fired {total} times and appears to oscillate near its threshold. "
                        "Increase the hold duration or add hysteresis at the source."
                    ),
                    "scope": {
                        "source": source,
                        "rule_name": rule_name,
                        "total": total,
                        "duplicates": duplicates,
                    },
                    "confidence": 0.72,
                    "estimated_notifications": duplicates,
                    "estimated_minutes_saved": duplicates * _MINUTES_PER_AVOIDED_NOTIFICATION,
                    "action_available": False,
                    "reversible": False,
                }
            )

    suggestions.extend(
        await _digest_suggestions(
            session,
            window_days=window_days,
            noisy_rules=noisy_rules,
            alert_rule_names=alert_rule_names,
        )
    )

    priority_order = {"high": 0, "medium": 1, "low": 2}
    suggestions.sort(
        key=lambda item: (
            priority_order.get(str(item["priority"]), 3),
            -int(item["estimated_notifications"]),
            str(item["id"]),
        )
    )
    return suggestions[:20]


def _serialize_action(action: NoiseReductionAction) -> dict[str, Any]:
    return {
        "id": int(action.id),
        "suggestion_id": action.suggestion_id,
        "action_type": action.action_type,
        "resource_type": action.resource_type,
        "resource_id": action.resource_id,
        "estimated_notifications": int(action.estimated_notifications or 0),
        "status": action.status,
        "actor": action.actor,
        "created_at": utc_isoformat(action.created_at),
        "undone_at": utc_isoformat(action.undone_at),
        "undo_available": action.status == "applied",
    }


async def _current_window_state(
    session: AsyncSession, *, window_days: int
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Current-window metrics, sources, and suggestions.

    Shared by the dashboard view and by suggestion revalidation, so applying a
    suggestion does not also pay for the previous-window comparison or the
    recent-actions list it never reads.
    """
    now = utcnow()
    start = now - timedelta(days=window_days)
    current = await _window_metrics(session, start=start, end=now, include_sources=True)
    rule_keys = current.pop("_rule_keys")
    alert_rule_names = set(current.pop("_alert_rule_names", set()))
    sources = list(current.pop("sources", []))
    noisy_rules = list(current.pop("noisy_rules", []))
    suggestions = await _build_suggestions(
        session,
        window_days=window_days,
        summary=current,
        sources=sources,
        noisy_rules=noisy_rules,
        rule_keys=rule_keys,
        alert_rule_names=alert_rule_names,
    )
    return current, sources, noisy_rules, suggestions


async def get_noise_center(session: AsyncSession, *, window_days: int = 7) -> dict[str, Any]:
    """Build the noise dashboard from existing alert and decision data."""
    window_days = max(1, min(90, int(window_days)))
    now = utcnow()
    start = now - timedelta(days=window_days)
    previous_start = start - timedelta(days=window_days)
    current, sources, noisy_rules, suggestions = await _current_window_state(session, window_days=window_days)
    previous = await _window_metrics(session, start=previous_start, end=start, include_sources=False)
    actions = list(
        (
            await session.execute(
                select(NoiseReductionAction)
                .order_by(NoiseReductionAction.created_at.desc(), NoiseReductionAction.id.desc())
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    return {
        "window_days": window_days,
        "summary": current,
        "previous": {
            **previous,
            "noise_rate_delta": round(float(current["noise_rate"]) - float(previous["noise_rate"]), 1),
            "avoided_delta": int(current["notifications_avoided"]) - int(previous["notifications_avoided"]),
        },
        "assumptions": {"minutes_per_avoided_notification": _MINUTES_PER_AVOIDED_NOTIFICATION},
        "sources": sources,
        "noisy_rules": noisy_rules,
        "suggestions": suggestions,
        "recent_actions": [_serialize_action(action) for action in actions],
    }


async def apply_noise_suggestion(
    session: AsyncSession,
    *,
    suggestion_id: str,
    window_days: int,
    actor: str,
) -> dict[str, Any]:
    """Revalidate and apply one current recommendation."""
    existing = (
        await session.execute(
            select(NoiseReductionAction)
            .where(
                NoiseReductionAction.suggestion_id == suggestion_id,
                NoiseReductionAction.status == "applied",
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return {"changed": False, "reason": "already_applied", "action": _serialize_action(existing)}

    window_days = max(1, min(90, int(window_days)))
    _, _, _, suggestions = await _current_window_state(session, window_days=window_days)
    suggestion = next((item for item in suggestions if item["id"] == suggestion_id), None)
    if suggestion is None or not suggestion.get("action_available"):
        return {"changed": False, "reason": "suggestion_not_available"}

    kind = str(suggestion["kind"])
    scope = dict(suggestion.get("scope") or {})
    before_state: dict[str, object]
    after_state: dict[str, object]
    resource_type: str
    resource_id: int
    resource_name: str

    if kind == "duplicate_filter":
        rule_id = int(scope["rule_id"])
        rule = await session.get(ForwardRule, rule_id)
        if rule is None or not rule.enabled or str(rule.match_duplicate or "all") != "all":
            return {"changed": False, "reason": "rule_state_changed"}
        before_state = {"match_duplicate": str(rule.match_duplicate or "all")}
        updated = await update_forward_rule(session, rule_id, {"match_duplicate": "new"})
        if updated is None:
            return {"changed": False, "reason": "rule_not_found"}
        after_state = {"match_duplicate": "new"}
        resource_type = "forward_rule"
        resource_id = rule_id
        resource_name = rule.name
        summary = f"Noise Center changed {rule.name} to forward new alerts only"
    elif kind == "temporary_silence":
        source = str(scope["source"])
        match_payload = str(scope["match_payload"])
        duration_hours = max(1, min(168, int(scope.get("duration_hours") or 24)))
        silence = await create_silence(
            session,
            match_source=source,
            match_payload=match_payload,
            comment=f"Noise Center trial for {scope.get('rule_name') or source}",
            created_by=actor,
            expires_at=utcnow() + timedelta(hours=duration_hours),
        )
        before_state = {}
        after_state = {
            "silence_id": int(silence.id),
            "match_source": source,
            "match_payload": match_payload,
            "duration_hours": duration_hours,
        }
        resource_type = "silence"
        resource_id = int(silence.id)
        resource_name = str(scope.get("rule_name") or source)
        summary = f"Noise Center created a {duration_hours}-hour trial silence for {resource_name}"
    elif kind == "digest":
        rule_name = str(scope["rule_name"])
        window_minutes = int(scope.get("window_minutes") or DIGEST_WINDOW_MINUTES_DEFAULT)
        payload = {
            "name": f"digest {window_minutes}m: {rule_name}",
            "action": DIGEST,
            "action_value": str(window_minutes),
            "match_rule_name": rule_name,
            "enabled": True,
            "comment": f"Noise Center {utc_isoformat(utcnow())}: {scope.get('duplicate_rate')}% repeats "
            f"over {scope.get('total')} alerts",
        }
        # Validated by the same function the API uses: a rule this page writes
        # must be refusable for the same reasons a hand-written one is.
        problem = validate_inbound_rule(payload)
        if problem is not None:
            return {"changed": False, "reason": "invalid_inbound_rule"}
        existing_rule = (
            await session.execute(
                select(InboundRule)
                .where(InboundRule.action == DIGEST, InboundRule.match_rule_name == rule_name)
                .order_by(InboundRule.id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing_rule is None:
            inbound = await create_inbound_rule(session, payload, actor=actor)
        else:
            # Re-applying after an undo re-enables the row it disabled rather
            # than leaving two rules with the same name and the same effect.
            updated_rule = await update_inbound_rule(
                session, int(existing_rule.id), {"enabled": True, "action_value": str(window_minutes)}
            )
            if updated_rule is None:
                return {"changed": False, "reason": "rule_not_found"}
            inbound = updated_rule
        before_state = {}
        after_state = {
            "inbound_rule_id": int(inbound.id),
            "match_rule_name": rule_name,
            "window_minutes": window_minutes,
        }
        resource_type = "inbound_rule"
        resource_id = int(inbound.id)
        resource_name = inbound.name
        summary = f"Noise Center will deliver {rule_name} as a {window_minutes}-minute digest"
    else:
        return {"changed": False, "reason": "unsupported_suggestion"}

    action = NoiseReductionAction(
        suggestion_id=suggestion_id,
        action_type=kind,
        resource_type=resource_type,
        resource_id=resource_id,
        before_state=before_state,
        after_state=after_state,
        estimated_notifications=int(suggestion.get("estimated_notifications") or 0),
        status="applied",
        actor=(actor or "operator")[:100],
    )
    session.add(action)
    add_audit(
        session,
        resource_type,
        resource_id,
        resource_name,
        "noise_optimized",
        summary,
        actor=actor,
    )
    await session.commit()
    if resource_type == "inbound_rule":
        # Every worker caches inbound rules; without this the digest starts
        # applying a TTL later, and the operator watches cards keep arriving.
        await publish_inbound_rules_invalidation()
    return {"changed": True, "action": _serialize_action(action)}


async def undo_noise_action(session: AsyncSession, *, action_id: int, actor: str) -> dict[str, Any]:
    """Undo an optimization only when its target still matches the applied state."""
    action = (
        await session.execute(
            select(NoiseReductionAction).where(NoiseReductionAction.id == action_id).with_for_update()
        )
    ).scalar_one_or_none()
    if action is None:
        return {"changed": False, "reason": "action_not_found"}
    if action.status != "applied":
        return {"changed": False, "reason": "already_undone", "action": _serialize_action(action)}

    resource_name = str(action.resource_id or action.id)
    if action.action_type == "duplicate_filter":
        rule = await session.get(ForwardRule, action.resource_id)
        expected = str(action.after_state.get("match_duplicate") or "new")
        previous = str(action.before_state.get("match_duplicate") or "all")
        if rule is None:
            return {"changed": False, "reason": "rule_not_found"}
        if str(rule.match_duplicate or "all") != expected:
            return {"changed": False, "reason": "rule_state_changed"}
        await update_forward_rule(session, int(rule.id), {"match_duplicate": previous})
        resource_name = rule.name
    elif action.action_type == "digest":
        inbound = await session.get(InboundRule, action.resource_id)
        if inbound is None:
            return {"changed": False, "reason": "rule_not_found"}
        if not inbound.enabled or str(inbound.action or "") != DIGEST:
            return {"changed": False, "reason": "rule_state_changed"}
        # Disabled rather than deleted: the row carries the evidence that made
        # it, and an operator who wanted it back would have to retype that.
        await update_inbound_rule(session, int(inbound.id), {"enabled": False})
        resource_name = inbound.name
    elif action.action_type == "temporary_silence":
        silence = await session.get(Silence, action.resource_id)
        if silence is None:
            return {"changed": False, "reason": "silence_not_found"}
        if silence.lifted_at is not None:
            return {"changed": False, "reason": "silence_state_changed"}
        await lift_silence(session, int(silence.id))
        resource_name = silence.comment or str(silence.id)
    else:
        return {"changed": False, "reason": "unsupported_action"}

    action.status = "undone"
    action.undone_at = utcnow()
    add_audit(
        session,
        action.resource_type,
        action.resource_id,
        resource_name,
        "noise_undo",
        f"Noise Center optimization was undone: {resource_name}",
        actor=actor,
    )
    await session.commit()
    if action.resource_type == "inbound_rule":
        await publish_inbound_rules_invalidation()
    return {"changed": True, "action": _serialize_action(action)}

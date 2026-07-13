"""Noise-reduction analytics, recommendations, and reversible actions."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import case, distinct, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utc_isoformat, utcnow
from models import DecisionTrace, ForwardRule, NoiseReductionAction, Silence, WebhookEvent
from services.forwarding.rules import update_forward_rule
from services.incidents.grouping import is_recovery_payload
from services.operations.audit_logger import add_audit
from services.silences.store import create_silence, lift_silence, list_silences
from services.webhooks.rule_audit import get_rule_audit

_NOISE_SKIP_CODES = frozenset({"cooldown", "duplicate_no_rule", "noise_suppressed", "silenced"})
_MINUTES_PER_AVOIDED_NOTIFICATION = 3
_MAX_SOURCES = 12
_MAX_RECOVERY_SAMPLE = 20_000


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
    rule_keys: dict[tuple[str, str], str] = {}
    for source, parsed_data, ai_analysis in payload_rows:
        source_name = str(source or "unknown").strip()
        if is_recovery_payload(parsed_data, ai_analysis):
            recoveries += 1
            recovery_by_source[source_name] = recovery_by_source.get(source_name, 0) + 1
        identity = _rule_identity(parsed_data)
        if identity is not None:
            key, rule_name = identity
            rule_keys.setdefault((source_name, rule_name), key)

    result: dict[str, Any] = {
        "total": total,
        "duplicates": duplicates,
        "duplicate_rate": _pct(duplicates, total),
        "noise_events": noise_events,
        "noise_rate": _pct(noise_events, total),
        "recoveries": recoveries,
        "recovery_rate": _pct(recoveries, recovery_denominator),
        "recovery_sampled": recovery_sampled,
        "recovery_sample_size": len(payload_rows),
        "forwarded": forwarded,
        "notifications_avoided": filtered,
        "estimated_minutes_saved": filtered * _MINUTES_PER_AVOIDED_NOTIFICATION,
        "skip_breakdown": skip_breakdown,
        "_rule_keys": rule_keys,
    }
    if not include_sources:
        return result

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


async def _build_suggestions(
    session: AsyncSession,
    *,
    window_days: int,
    summary: dict[str, Any],
    sources: list[dict[str, Any]],
    rule_keys: dict[tuple[str, str], str],
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


async def get_noise_center(session: AsyncSession, *, window_days: int = 7) -> dict[str, Any]:
    """Build the noise dashboard from existing alert and decision data."""
    window_days = max(1, min(90, int(window_days)))
    now = utcnow()
    start = now - timedelta(days=window_days)
    previous_start = start - timedelta(days=window_days)
    current = await _window_metrics(session, start=start, end=now, include_sources=True)
    previous = await _window_metrics(session, start=previous_start, end=start, include_sources=False)
    rule_keys = current.pop("_rule_keys")
    previous.pop("_rule_keys", None)
    sources = list(current.pop("sources", []))
    suggestions = await _build_suggestions(
        session,
        window_days=window_days,
        summary=current,
        sources=sources,
        rule_keys=rule_keys,
    )
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

    center = await get_noise_center(session, window_days=window_days)
    suggestion = next((item for item in center["suggestions"] if item["id"] == suggestion_id), None)
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
    return {"changed": True, "action": _serialize_action(action)}

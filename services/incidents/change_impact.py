"""Deterministic before/after impact assessment for normalized changes."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utc_isoformat, utcnow
from models import ChangeEvent, Incident, WebhookEvent
from services.incidents.grouping import (
    _correlation_dimensions,
    _event_rule_name,
    is_recovery_payload,
)

_IMPACT_WINDOW_MINUTES = 30
_MAX_WINDOW_EVENTS = 2_000
_MAX_WINDOW_INCIDENTS = 200
_ROLLBACK_LOOKAHEAD_HOURS = 2


def _change_dimensions(change: ChangeEvent) -> dict[str, str]:
    return {
        key: value
        for key, raw in (
            ("service", change.service),
            ("environment", change.environment),
            ("project", change.project),
            ("region", change.region),
        )
        if (value := str(raw or "").strip().lower())
    }


def _matches_dimensions(candidate: dict[str, str], expected: dict[str, str]) -> bool:
    """Require the strongest available identity while rejecting conflicts."""
    if not candidate or not expected:
        return False
    if expected.get("service"):
        return candidate.get("service") == expected["service"] and not any(
            candidate.get(key) and candidate[key] != value for key, value in expected.items() if key != "service"
        )
    shared = {key for key, value in expected.items() if candidate.get(key) == value}
    conflicts = {key for key, value in expected.items() if candidate.get(key) and candidate[key] != value}
    return bool(shared) and not conflicts


def _event_identity(event: WebhookEvent) -> str:
    return str(event.dedup_key or "").strip() or "|".join(
        (
            str(event.source or "unknown"),
            _event_rule_name(event) or str(event.id),
        )
    )


def _rollback_marker(change: ChangeEvent) -> bool:
    status = str(change.status or "").strip().lower()
    if any(marker in status for marker in ("rollback", "rolled_back", "reverted")):
        return True
    details = change.details if isinstance(change.details, dict) else {}
    action = str(details.get("action") or details.get("operation") or "").strip().lower()
    return any(marker in action for marker in ("rollback", "revert"))


def _impact_level(score: float) -> str:
    if score >= 0.65:
        return "high"
    if score >= 0.30:
        return "medium"
    return "low"


def _assessment_summary(
    *,
    alert_delta: int,
    new_identity_count: int,
    linked_incident_count: int,
    collecting: bool,
) -> str:
    prefix = "Preliminary: " if collecting else ""
    if alert_delta > 0 and new_identity_count:
        return (
            f"{prefix}the post-change window contains {alert_delta} more alerts, including "
            f"{new_identity_count} new alert identities; this is an association, not proof of causation"
        )
    if alert_delta > 0:
        return (
            f"{prefix}the post-change window contains {alert_delta} more alerts; "
            "this is an association, not proof of causation"
        )
    if linked_incident_count:
        return f"{prefix}{linked_incident_count} incident(s) started near the change and may be related"
    if alert_delta < 0:
        return f"{prefix}the post-change window contains {abs(alert_delta)} fewer alerts"
    return f"{prefix}no material alert-volume change is visible in the observation window"


async def assess_change_impact(
    session: AsyncSession,
    change: ChangeEvent,
    *,
    window_minutes: int = _IMPACT_WINDOW_MINUTES,
) -> dict[str, Any]:
    """Compare matching alerts before/after a change and return bounded evidence."""
    window_minutes = max(5, min(int(window_minutes), 120))
    window = timedelta(minutes=window_minutes)
    expected = _change_dimensions(change)
    start = change.started_at - window
    planned_end = change.started_at + window
    now = utcnow()
    observed_end = min(planned_end, now)

    # Fetch each side of the change instant separately: one ascending query
    # over the whole window spends its entire row budget on the earliest rows,
    # so a busy window drops the "after" side and biases alert_delta negative
    # (a bad deploy would score as an improvement).
    side_limit = max(1, _MAX_WINDOW_EVENTS // 2)
    before_rows = list(
        (
            await session.execute(
                select(WebhookEvent)
                .where(
                    WebhookEvent.timestamp >= start,
                    WebhookEvent.timestamp < change.started_at,
                )
                .order_by(WebhookEvent.timestamp.desc(), WebhookEvent.id.desc())
                .limit(side_limit)
            )
        )
        .scalars()
        .all()
    )
    before_rows.reverse()
    after_rows = list(
        (
            await session.execute(
                select(WebhookEvent)
                .where(
                    WebhookEvent.timestamp >= change.started_at,
                    WebhookEvent.timestamp <= observed_end,
                )
                .order_by(WebhookEvent.timestamp, WebhookEvent.id)
                .limit(side_limit)
            )
        )
        .scalars()
        .all()
    )
    window_truncated = len(before_rows) >= side_limit or len(after_rows) >= side_limit
    before = [event for event in before_rows if _matches_dimensions(_correlation_dimensions(event), expected)]
    after = [event for event in after_rows if _matches_dimensions(_correlation_dimensions(event), expected)]
    matching_events = before + after
    before_identities = {_event_identity(event) for event in before}
    after_identities = {_event_identity(event) for event in after}
    new_identities = after_identities - before_identities
    before_high = sum(str(event.importance or "").lower() in {"high", "critical"} for event in before)
    after_high = sum(str(event.importance or "").lower() in {"high", "critical"} for event in after)

    nearby_incidents = list(
        (
            await session.execute(
                select(Incident)
                .where(
                    Incident.started_at >= change.started_at,
                    Incident.started_at <= change.started_at + timedelta(hours=1),
                )
                .order_by(Incident.started_at, Incident.id)
                .limit(_MAX_WINDOW_INCIDENTS)
            )
        )
        .scalars()
        .all()
    )
    linked_incidents = [
        incident
        for incident in nearby_incidents
        if _matches_dimensions(
            {
                str(key): str(value).strip().lower()
                for key, value in (incident.correlation_dimensions or {}).items()
                if value
            },
            expected,
        )
    ]

    rollback_candidates = list(
        (
            await session.execute(
                select(ChangeEvent)
                .where(
                    ChangeEvent.id != change.id,
                    ChangeEvent.started_at > change.started_at,
                    ChangeEvent.started_at <= change.started_at + timedelta(hours=_ROLLBACK_LOOKAHEAD_HOURS),
                )
                .order_by(ChangeEvent.started_at, ChangeEvent.id)
                .limit(100)
            )
        )
        .scalars()
        .all()
    )
    rollback = next(
        (
            candidate
            for candidate in rollback_candidates
            if _matches_dimensions(_change_dimensions(candidate), expected) and _rollback_marker(candidate)
        ),
        None,
    )
    recovered_after_rollback = False
    if rollback is not None:
        recovery_events = list(
            (
                await session.execute(
                    select(WebhookEvent)
                    .where(
                        WebhookEvent.timestamp >= rollback.started_at,
                        WebhookEvent.timestamp <= rollback.started_at + window,
                    )
                    .order_by(WebhookEvent.timestamp, WebhookEvent.id)
                    .limit(500)
                )
            )
            .scalars()
            .all()
        )
        recovered_after_rollback = any(
            _matches_dimensions(_correlation_dimensions(event), expected)
            and is_recovery_payload(event.parsed_data, event.ai_analysis)
            for event in recovery_events
        )

    alert_delta = len(after) - len(before)
    high_delta = after_high - before_high
    increase_ratio = max(0.0, alert_delta / max(1, len(before)))
    score = min(0.45, increase_ratio * 0.15)
    score += min(0.20, len(new_identities) * 0.05)
    score += min(0.20, max(0, high_delta) * 0.10)
    score += min(0.15, len(linked_incidents) * 0.10)
    if recovered_after_rollback:
        score = max(score, 0.75)
    score = min(score, 1.0)
    collecting = now < planned_end
    insufficient_data = not expected or (not collecting and len(matching_events) < 2 and not linked_incidents)

    evidence: list[dict[str, object]] = []
    if alert_delta:
        evidence.append({"code": "alert_volume_delta", "value": alert_delta})
    if new_identities:
        evidence.append({"code": "new_alert_identities", "value": len(new_identities)})
    if high_delta:
        evidence.append({"code": "high_severity_delta", "value": high_delta})
    if linked_incidents:
        evidence.append({"code": "linked_incidents", "value": len(linked_incidents)})
    if rollback is not None:
        evidence.append(
            {
                "code": "rollback_detected",
                "value": rollback.external_id,
            }
        )
    if recovered_after_rollback:
        evidence.append({"code": "recovered_after_rollback", "value": True})
    if window_truncated:
        evidence.append({"code": "window_truncated", "value": True})
    if not expected:
        evidence.append({"code": "missing_change_identity", "value": True})
    elif insufficient_data:
        evidence.append({"code": "insufficient_matching_samples", "value": len(matching_events)})

    identity_strength = min(1.0, len(expected) / 2)
    window_strength = min(1.0, max(0.0, (observed_end - change.started_at).total_seconds()) / window.total_seconds())
    signal_strength = min(1.0, (len(before) + len(after) + len(linked_incidents)) / 10)
    confidence = round(0.25 + 0.30 * identity_strength + 0.25 * window_strength + 0.20 * signal_strength, 3)

    if not expected:
        summary = "The change has no service or project identity, so impact cannot be assessed safely"
    elif insufficient_data:
        summary = "There are not enough matching alert or incident samples to assess impact"
    else:
        summary = _assessment_summary(
            alert_delta=alert_delta,
            new_identity_count=len(new_identities),
            linked_incident_count=len(linked_incidents),
            collecting=collecting,
        )
    level = (
        "unknown"
        if insufficient_data or (collecting and not matching_events and not linked_incidents)
        else _impact_level(score)
    )

    return {
        "strategy": "before_after_v1",
        "status": "insufficient_data" if insufficient_data else "collecting" if collecting else "complete",
        "level": level,
        "score": round(score, 3),
        "confidence": min(confidence, 1.0),
        "summary": summary,
        "identity_dimensions": expected,
        "window_minutes": window_minutes,
        "truncated": window_truncated,
        "before_alert_count": len(before),
        "after_alert_count": len(after),
        "alert_delta": alert_delta,
        "before_high_count": before_high,
        "after_high_count": after_high,
        "new_identity_count": len(new_identities),
        "linked_incident_count": len(linked_incidents),
        "linked_incident_ids": [int(incident.id) for incident in linked_incidents[:10]],
        "rollback_detected": rollback is not None,
        "rollback_change_id": int(rollback.id) if rollback is not None else None,
        "recovered_after_rollback": recovered_after_rollback,
        "observed_through": utc_isoformat(observed_end),
        "evidence": evidence,
    }


async def get_change_impact(
    session: AsyncSession,
    change_id: int,
    *,
    window_minutes: int = _IMPACT_WINDOW_MINUTES,
) -> dict[str, Any] | None:
    """Return one normalized change and its calculated impact."""
    change = await session.get(ChangeEvent, change_id)
    if change is None:
        return None
    return {
        "change_id": int(change.id),
        "external_id": change.external_id,
        "source": change.source,
        "service": change.service,
        "environment": change.environment,
        "started_at": utc_isoformat(change.started_at),
        "impact_assessment": await assess_change_impact(
            session,
            change,
            window_minutes=window_minutes,
        ),
    }

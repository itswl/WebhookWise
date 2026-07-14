"""Periodic grouping of related alerts into operational incidents."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utcnow
from core.logger import get_logger
from db.session import acquire_advisory_xact_lock, session_scope
from models import Incident, IncidentMember, WebhookEvent
from services.incidents.summary import queue_summary_if_needed

logger = get_logger("incidents.grouping")

_INCIDENT_WINDOW_MINUTES = 15
_INCIDENT_QUIET_MINUTES = 10
_SCAN_LOOKBACK_MINUTES = 4320  # 72 h initial/backfill safety window
_MAX_MEMBERS_PER_INCIDENT = 200
_MAX_INCIDENTS_PER_SCAN = 200
_RECOVERY_VALUES = {"resolved", "recovered", "recovery", "ok", "normal", "healthy", "inactive", "恢复"}
_DIMENSION_ALIASES: dict[str, tuple[str, ...]] = {
    "service": ("service", "service_name", "servicename", "application", "app"),
    "project": ("project", "project_name", "projectname"),
    "environment": ("environment", "env", "cluster"),
    "region": ("region", "region_id", "regionid"),
}


def _event_rule_name(event: WebhookEvent) -> str:
    """Extract the upstream rule name used as part of the incident identity."""
    parsed = event.parsed_data or {}
    if isinstance(parsed, dict):
        return str(parsed.get("RuleName") or parsed.get("AlertName") or parsed.get("alert_name") or "").strip()[:200]
    return ""


def _flatten_payload(payload: dict[str, object]) -> dict[str, object]:
    flattened: dict[str, object] = {}
    queue: list[dict[str, object]] = [payload]
    while queue and len(flattened) < 100:
        current = queue.pop(0)
        for key, value in current.items():
            normalized = str(key).replace("-", "_").lower()
            flattened.setdefault(normalized, value)
            if isinstance(value, dict) and normalized in {
                "labels",
                "commonlabels",
                "common_labels",
                "metadata",
                "dimensions",
                "tags",
            }:
                queue.append(value)
            elif normalized == "alerts" and isinstance(value, list):
                queue.extend(item for item in value[:5] if isinstance(item, dict))
    return flattened


def _correlation_dimensions(event: WebhookEvent) -> dict[str, str]:
    """Extract stable service identity without depending on one vendor payload."""
    parsed = event.parsed_data or {}
    if not isinstance(parsed, dict):
        return {}
    flattened = _flatten_payload(parsed)
    dimensions: dict[str, str] = {}
    for dimension, aliases in _DIMENSION_ALIASES.items():
        for alias in aliases:
            value = flattened.get(alias)
            if value is None or isinstance(value, (dict, list)):
                continue
            normalized = str(value).strip().lower()
            if normalized:
                dimensions[dimension] = normalized[:200]
                break
    return dimensions


def is_recovery_payload(
    parsed_data: Mapping[str, object] | None,
    ai_analysis: Mapping[str, object] | None,
) -> bool:
    """Return whether normalized alert data represents a recovery signal."""
    parsed = parsed_data or {}
    flattened = _flatten_payload(parsed) if isinstance(parsed, dict) else {}
    for key in ("status", "state", "alert_status", "event_status", "phase"):
        value = str(flattened.get(key) or "").strip().lower()
        if value in _RECOVERY_VALUES or "恢复" in value:
            return True
    analysis = ai_analysis or {}
    if isinstance(analysis, dict):
        event_type = str(analysis.get("event_type") or analysis.get("type") or "").strip().lower()
        if event_type in _RECOVERY_VALUES or "恢复" in event_type:
            return True
    return False


def _is_recovery_event(event: WebhookEvent) -> bool:
    return is_recovery_payload(event.parsed_data, event.ai_analysis)


def _dimension_score(left: Mapping[str, str], right: Mapping[str, object]) -> float:
    shared = {key for key, value in left.items() if value and str(right.get(key) or "") == value}
    if "service" in shared and ("project" in shared or "environment" in shared):
        return 0.9
    if {"service", "region"}.issubset(shared):
        return 0.85
    if {"project", "environment"}.issubset(shared):
        return 0.8
    if "service" in shared:
        return 0.72
    return 0.0


def _dimensions_conflict(left: Mapping[str, str], right: Mapping[str, object]) -> bool:
    identity_keys = {"service", "project", "environment", "region"}
    return any(key in left and right.get(key) and str(right[key]) != left[key] for key in identity_keys)


def _event_pair_score(left: WebhookEvent, right: WebhookEvent) -> float:
    same_source = str(left.source or "") == str(right.source or "")
    left_rule = _event_rule_name(left)
    right_rule = _event_rule_name(right)
    left_dimensions = _correlation_dimensions(left)
    right_dimensions = _correlation_dimensions(right)
    if _dimensions_conflict(left_dimensions, right_dimensions):
        return 0.0
    if same_source and left_rule == right_rule:
        return 1.0
    dimension_score = _dimension_score(left_dimensions, right_dimensions)
    if dimension_score and (same_source or dimension_score >= 0.8):
        return dimension_score
    if same_source and (not left_rule or not right_rule):
        return 0.65
    return 0.0


def _incident_correlation_score(event: WebhookEvent, incident: Incident) -> float:
    same_source = str(event.source or "") == str(incident.source or "")
    event_dimensions = _correlation_dimensions(event)
    incident_dimensions = incident.correlation_dimensions or {}
    if _dimensions_conflict(event_dimensions, incident_dimensions):
        return 0.0
    if same_source and _incident_rule_matches(_event_rule_name(event), incident):
        return 1.0
    dimension_score = _dimension_score(event_dimensions, incident_dimensions)
    if dimension_score and (same_source or dimension_score >= 0.8):
        return dimension_score
    return 0.65 if same_source and not _event_rule_name(event) else 0.0


async def run_incident_grouping() -> dict[str, Any]:
    """Group one bounded batch, close quiet incidents, then run post-commit work."""
    now = utcnow()
    lookback_cutoff = now - timedelta(minutes=_SCAN_LOOKBACK_MINUTES)
    outbox_ids: list[int] = []

    async with session_scope() as session:
        # Only one scheduler may mutate incident membership at a time. This is a
        # no-op on SQLite tests and transaction-scoped on PostgreSQL.
        await acquire_advisory_xact_lock(session, "incident_grouping")

        unassigned_stmt = (
            select(WebhookEvent)
            .where(WebhookEvent.timestamp >= lookback_cutoff)
            .where(~exists(select(IncidentMember.id).where(IncidentMember.event_id == WebhookEvent.id)))
            # Read the newest bounded window so old singleton candidates cannot
            # permanently starve newer pairs. Reverse below for chronological
            # processing within the selected window.
            .order_by(WebhookEvent.timestamp.desc(), WebhookEvent.id.desc())
            .limit(_MAX_INCIDENTS_PER_SCAN)
        )
        unassigned = list((await session.execute(unassigned_stmt)).scalars().all())
        unassigned.reverse()

        active_incidents = list(
            (
                await session.execute(
                    select(Incident)
                    .where(
                        Incident.status == "active",
                        or_(
                            Incident.updated_at.is_(None),
                            Incident.updated_at > now - timedelta(minutes=_INCIDENT_QUIET_MINUTES),
                        ),
                    )
                    .order_by(Incident.started_at.asc())
                )
            )
            .scalars()
            .all()
        )
        recoverable_incidents = list(
            (
                await session.execute(
                    select(Incident)
                    .where(
                        Incident.status.in_(["active", "quiet"]),
                        Incident.started_at >= lookback_cutoff,
                    )
                    .order_by(Incident.started_at.desc())
                    .limit(_MAX_INCIDENTS_PER_SCAN)
                )
            )
            .scalars()
            .all()
        )

        created_incidents: list[Incident] = []
        candidates: list[WebhookEvent] = []
        updated = 0
        recovered = 0
        for event in unassigned:
            if _is_recovery_event(event):
                match = _find_recovery_incident(event, recoverable_incidents)
                if match is not None and _add_event_to_incident(session, match, event):
                    _resolve_incident_from_recovery(match, event)
                    recovered += 1
                continue
            match = _find_matching_incident(event, active_incidents)
            if match is None:
                candidate = _find_matching_candidate(event, candidates)
                if candidate is None:
                    candidates.append(event)
                    continue
                candidates.remove(candidate)
                match = _create_incident_from_event(candidate)
                session.add(match)
                await session.flush()
                active_incidents.append(match)
                recoverable_incidents.append(match)
                created_incidents.append(match)
                _add_event_to_incident(session, match, candidate)

            if _add_event_to_incident(session, match, event) and match not in created_incidents:
                updated += 1

        closed = await _close_quiet_incidents(session, now)

        if created_incidents:
            from services.incidents.notifications import queue_incident_notifications

            # Preserve the prior anti-spam behavior while making each selected
            # notification durable in the forwarding outbox transaction.
            outbox_ids = await queue_incident_notifications(session, created_incidents[:3])

        from services.incidents.notifications import queue_sla_breach_notifications

        outbox_ids.extend(await queue_sla_breach_notifications(session, now))

        stats = {
            "scanned": len(unassigned),
            "created": len(created_incidents),
            "updated": updated,
            "closed": closed,
        }

    # The incident transaction is committed before any network or LLM work.
    if outbox_ids:
        from services.forwarding.outbox_scheduling import schedule_forward_outbox_many

        await schedule_forward_outbox_many(outbox_ids)

    from services.incidents.summary import run_pending_incident_summaries

    await run_pending_incident_summaries()

    if recovered or any(stats[key] for key in ("created", "updated", "closed")):
        logger.info(
            "[Incidents] Grouping tick: scanned=%d created=%d updated=%d closed=%d recovered=%d",
            stats["scanned"],
            stats["created"],
            stats["updated"],
            stats["closed"],
            recovered,
        )
    return stats


def _find_matching_incident(
    event: WebhookEvent,
    incidents: list[Incident],
) -> Incident | None:
    """Return the oldest compatible, non-full active incident."""
    event_timestamp = event.timestamp or utcnow()
    window_start = event_timestamp - timedelta(minutes=_INCIDENT_WINDOW_MINUTES)
    window_end = event_timestamp + timedelta(minutes=_INCIDENT_WINDOW_MINUTES)
    best: tuple[float, Incident] | None = None
    for incident in incidents:
        if incident.status != "active" or incident.alert_count >= _MAX_MEMBERS_PER_INCIDENT:
            continue
        last_ts = incident.updated_at or incident.started_at
        if last_ts is not None and not window_start <= last_ts <= window_end:
            continue
        score = _incident_correlation_score(event, incident)
        if score >= 0.65 and (best is None or score > best[0]):
            best = (score, incident)
    return best[1] if best else None


def _find_matching_candidate(event: WebhookEvent, candidates: list[WebhookEvent]) -> WebhookEvent | None:
    """Return an earlier compatible unassigned alert without persisting a singleton incident."""
    event_timestamp = event.timestamp or utcnow()
    best: tuple[float, WebhookEvent] | None = None
    for candidate in reversed(candidates):
        candidate_timestamp = candidate.timestamp or event_timestamp
        if abs((event_timestamp - candidate_timestamp).total_seconds()) > _INCIDENT_WINDOW_MINUTES * 60:
            continue
        score = _event_pair_score(event, candidate)
        if score >= 0.65 and (best is None or score > best[0]):
            best = (score, candidate)
    return best[1] if best else None


def _find_recovery_incident(event: WebhookEvent, incidents: list[Incident]) -> Incident | None:
    """Match recovery signals against recent active or quiet incidents."""
    event_timestamp = event.timestamp or utcnow()
    event_rule = _event_rule_name(event)
    event_dimensions = _correlation_dimensions(event)
    if not event_rule and not event_dimensions:
        return None
    best: tuple[float, Incident] | None = None
    for incident in incidents:
        if incident.status not in {"active", "quiet"} or incident.alert_count >= _MAX_MEMBERS_PER_INCIDENT:
            continue
        if incident.started_at > event_timestamp:
            continue
        score = (
            _incident_correlation_score(event, incident)
            if event_rule
            else _dimension_score(event_dimensions, incident.correlation_dimensions or {})
        )
        if score >= 0.8 and (best is None or score > best[0]):
            best = (score, incident)
    return best[1] if best else None


def _incident_rule_matches(event_rule: str, incident: Incident) -> bool:
    if not event_rule:
        return True
    title = str(incident.title or "")
    separator = " — "
    if separator in title:
        return title.split(separator, 1)[1] == event_rule
    return True


def _create_incident_from_event(event: WebhookEvent) -> Incident:
    rule_name = _event_rule_name(event)
    title = f"{event.source or 'unknown'} incident"
    if rule_name:
        title = f"{title} — {rule_name}"
    timestamp = event.timestamp or utcnow()
    return Incident(
        title=title,
        status="active",
        source=event.source,
        started_at=timestamp,
        updated_at=timestamp,
        alert_count=0,
        top_importance=event.importance,
        workflow_status="open",
        correlation_dimensions=_correlation_dimensions(event),
        correlation_confidence=1.0,
    )


def _add_event_to_incident(
    session: AsyncSession,
    incident: Incident,
    event: WebhookEvent,
) -> bool:
    """Add one member and return whether the membership was accepted."""
    if incident.alert_count >= _MAX_MEMBERS_PER_INCIDENT or incident.id is None or event.id is None:
        return False
    score = _incident_correlation_score(event, incident)
    timestamp = event.timestamp or utcnow()
    session.add(
        IncidentMember(
            incident_id=incident.id,
            event_id=event.id,
            event_timestamp=timestamp,
        )
    )
    incident.alert_count += 1
    if score:
        incident.correlation_confidence = min(incident.correlation_confidence or score, score)
    incident.updated_at = max(incident.updated_at or timestamp, timestamp)
    if event.importance == "high" or incident.top_importance != "high":
        incident.top_importance = event.importance
    dimensions = dict(incident.correlation_dimensions or {})
    for key, value in _correlation_dimensions(event).items():
        dimensions.setdefault(key, value)
    incident.correlation_dimensions = dimensions
    if incident.source and incident.source != event.source:
        incident.source = "multiple"
    return True


def _resolve_incident_from_recovery(incident: Incident, event: WebhookEvent) -> None:
    timestamp = event.timestamp or utcnow()
    incident.status = "closed"
    incident.workflow_status = "resolved"
    incident.resolved_at = timestamp
    incident.ended_at = timestamp
    incident.updated_at = timestamp
    queue_summary_if_needed(incident, timestamp)


async def _close_quiet_incidents(session: AsyncSession, now: Any) -> int:
    """Mark quiet incidents and persist durable summary work state."""
    quiet_threshold = now - timedelta(minutes=_INCIDENT_QUIET_MINUTES)
    quiet_list = list(
        (
            await session.execute(
                select(Incident)
                .where(
                    Incident.status == "active",
                    Incident.updated_at.isnot(None),
                    Incident.updated_at <= quiet_threshold,
                )
                .order_by(Incident.updated_at.asc(), Incident.id.asc())
                .limit(_MAX_INCIDENTS_PER_SCAN)
            )
        )
        .scalars()
        .all()
    )
    for incident in quiet_list:
        incident.status = "quiet"
        incident.ended_at = now
        incident.updated_at = now
        queue_summary_if_needed(incident, now)
        logger.info(
            "[Incidents] Incident quieted id=%s title=%s alerts=%d",
            incident.id,
            incident.title[:80],
            incident.alert_count,
        )
    return len(quiet_list)

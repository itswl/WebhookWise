"""Periodic grouping of related alerts into operational incidents."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utcnow
from core.logger import get_logger
from db.session import acquire_advisory_xact_lock, session_scope
from models import Incident, IncidentMember, WebhookEvent

logger = get_logger("incidents.grouping")

_INCIDENT_WINDOW_MINUTES = 15
_INCIDENT_QUIET_MINUTES = 10
_SCAN_LOOKBACK_MINUTES = 4320  # 72 h initial/backfill safety window
_MAX_MEMBERS_PER_INCIDENT = 200
_MAX_INCIDENTS_PER_SCAN = 200


def _event_rule_name(event: WebhookEvent) -> str:
    """Extract the upstream rule name used as part of the incident identity."""
    parsed = event.parsed_data or {}
    if isinstance(parsed, dict):
        return str(parsed.get("RuleName") or parsed.get("AlertName") or parsed.get("alert_name") or "").strip()[:200]
    return ""


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

        created_incidents: list[Incident] = []
        candidates: list[WebhookEvent] = []
        updated = 0
        for event in unassigned:
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

    if any(stats[key] for key in ("created", "updated", "closed")):
        logger.info(
            "[Incidents] Grouping tick: scanned=%d created=%d updated=%d closed=%d",
            stats["scanned"],
            stats["created"],
            stats["updated"],
            stats["closed"],
        )
    return stats


def _find_matching_incident(
    event: WebhookEvent,
    incidents: list[Incident],
) -> Incident | None:
    """Return the oldest compatible, non-full active incident."""
    event_source = str(event.source or "")
    event_rule = _event_rule_name(event)
    event_timestamp = event.timestamp or utcnow()
    window_start = event_timestamp - timedelta(minutes=_INCIDENT_WINDOW_MINUTES)
    window_end = event_timestamp + timedelta(minutes=_INCIDENT_WINDOW_MINUTES)
    for incident in incidents:
        if incident.status != "active" or incident.alert_count >= _MAX_MEMBERS_PER_INCIDENT:
            continue
        if str(incident.source or "") != event_source:
            continue
        if not _incident_rule_matches(event_rule, incident):
            continue
        last_ts = incident.updated_at or incident.started_at
        if last_ts is not None and not window_start <= last_ts <= window_end:
            continue
        return incident
    return None


def _find_matching_candidate(event: WebhookEvent, candidates: list[WebhookEvent]) -> WebhookEvent | None:
    """Return an earlier compatible unassigned alert without persisting a singleton incident."""
    event_source = str(event.source or "")
    event_rule = _event_rule_name(event)
    event_timestamp = event.timestamp or utcnow()
    for candidate in reversed(candidates):
        if str(candidate.source or "") != event_source:
            continue
        if _event_rule_name(candidate) != event_rule:
            continue
        candidate_timestamp = candidate.timestamp or event_timestamp
        if abs((event_timestamp - candidate_timestamp).total_seconds()) <= _INCIDENT_WINDOW_MINUTES * 60:
            return candidate
    return None


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
    )


def _add_event_to_incident(
    session: AsyncSession,
    incident: Incident,
    event: WebhookEvent,
) -> bool:
    """Add one member and return whether the membership was accepted."""
    if incident.alert_count >= _MAX_MEMBERS_PER_INCIDENT or incident.id is None or event.id is None:
        return False
    timestamp = event.timestamp or utcnow()
    session.add(
        IncidentMember(
            incident_id=incident.id,
            event_id=event.id,
            event_timestamp=timestamp,
        )
    )
    incident.alert_count += 1
    incident.updated_at = max(incident.updated_at or timestamp, timestamp)
    if event.importance == "high" or incident.top_importance != "high":
        incident.top_importance = event.importance
    return True


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
        if incident.summary_analysis is None and incident.alert_count >= 2:
            incident.summary_status = "pending"
            incident.summary_attempts = 0
            incident.summary_next_attempt_at = now
            incident.summary_last_error = None
        elif incident.summary_analysis is None:
            incident.summary_status = "skipped"
            incident.summary_next_attempt_at = None
            incident.summary_last_error = "singleton incidents are not summarized"
        logger.info(
            "[Incidents] Incident quieted id=%s title=%s alerts=%d",
            incident.id,
            incident.title[:80],
            incident.alert_count,
        )
    return len(quiet_list)

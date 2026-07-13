"""Operator lifecycle, ownership, notes, feedback, and incident editing."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utc_isoformat, utcnow
from models import AnalysisFeedback, Incident, IncidentMember, OperationalNote, WebhookEvent
from services.operations.audit_logger import add_audit

TERMINAL_WORKFLOW_STATUSES = {"resolved", "ignored"}


async def get_resource(session: AsyncSession, resource_type: str, resource_id: int) -> WebhookEvent | Incident | None:
    if resource_type == "webhook_event":
        return await session.get(WebhookEvent, resource_id)
    if resource_type == "incident":
        return await session.get(Incident, resource_id)
    return None


def workflow_dict(resource: WebhookEvent | Incident) -> dict[str, Any]:
    return {
        "id": resource.id,
        "workflow_status": resource.workflow_status,
        "assignee": resource.assignee,
        "team": resource.team,
        "acknowledged_at": utc_isoformat(resource.acknowledged_at),
        "resolved_at": utc_isoformat(resource.resolved_at),
        "sla_due_at": utc_isoformat(resource.sla_due_at),
        "sla_breached": bool(
            resource.sla_due_at
            and resource.workflow_status not in TERMINAL_WORKFLOW_STATUSES
            and resource.sla_due_at <= utcnow()
        ),
    }


async def update_workflow(
    session: AsyncSession,
    *,
    resource_type: str,
    resource_id: int,
    changes: dict[str, Any],
) -> dict[str, Any] | None:
    """Apply one explicit operator workflow patch and audit the transition."""
    resource = await get_resource(session, resource_type, resource_id)
    if resource is None:
        return None

    now = utcnow()
    new_status = changes.get("workflow_status")
    if new_status:
        resource.workflow_status = str(new_status)
        if new_status in {"acknowledged", "in_progress"} and resource.acknowledged_at is None:
            resource.acknowledged_at = now
        if new_status in TERMINAL_WORKFLOW_STATUSES:
            resource.resolved_at = now
        elif new_status == "open":
            resource.resolved_at = None

        if isinstance(resource, Incident):
            if new_status in TERMINAL_WORKFLOW_STATUSES:
                resource.status = "closed"
                resource.ended_at = resource.ended_at or now
                _queue_summary_if_needed(resource, now)
            elif new_status == "open" and resource.status == "closed":
                resource.status = "active"
                resource.ended_at = None

    if "assignee" in changes:
        resource.assignee = str(changes.get("assignee") or "").strip() or None
    if "team" in changes:
        resource.team = str(changes.get("team") or "").strip() or None
    if changes.get("clear_sla"):
        resource.sla_due_at = None
    elif changes.get("sla_minutes") is not None:
        resource.sla_due_at = now + timedelta(minutes=int(changes["sla_minutes"]))

    label = str(getattr(resource, "title", None) or getattr(resource, "request_id", None) or resource_id)
    add_audit(
        session,
        resource_type,
        resource_id,
        label[:200],
        "workflow_updated",
        f"Workflow updated: status={resource.workflow_status}, assignee={resource.assignee or 'unassigned'}",
    )
    await session.commit()
    return workflow_dict(resource)


def _queue_summary_if_needed(incident: Incident, now: Any) -> None:
    if incident.summary_analysis is None and incident.alert_count >= 2:
        incident.summary_status = "pending"
        incident.summary_attempts = 0
        incident.summary_next_attempt_at = now
        incident.summary_last_error = None
    elif incident.summary_analysis is None:
        incident.summary_status = "skipped"
        incident.summary_next_attempt_at = None
        incident.summary_last_error = "singleton incidents are not summarized"


async def add_note(
    session: AsyncSession,
    *,
    resource_type: str,
    resource_id: int,
    body: str,
    actor: str,
) -> dict[str, Any] | None:
    resource = await get_resource(session, resource_type, resource_id)
    if resource is None:
        return None
    note = OperationalNote(
        resource_type=resource_type,
        resource_id=resource_id,
        body=body.strip(),
        actor=actor.strip() or "operator",
        created_at=utcnow(),
    )
    session.add(note)
    await session.flush()
    add_audit(
        session,
        resource_type,
        resource_id,
        str(getattr(resource, "title", resource_id))[:200],
        "note_added",
        f"Operator note added by {note.actor}",
    )
    await session.commit()
    return _note_dict(note)


async def list_notes(session: AsyncSession, *, resource_type: str, resource_id: int) -> list[dict[str, Any]] | None:
    if await get_resource(session, resource_type, resource_id) is None:
        return None
    notes = list(
        (
            await session.execute(
                select(OperationalNote)
                .where(
                    OperationalNote.resource_type == resource_type,
                    OperationalNote.resource_id == resource_id,
                )
                .order_by(OperationalNote.created_at.desc(), OperationalNote.id.desc())
                .limit(100)
            )
        )
        .scalars()
        .all()
    )
    return [_note_dict(note) for note in notes]


def _note_dict(note: OperationalNote) -> dict[str, Any]:
    return {
        "id": note.id,
        "body": note.body,
        "actor": note.actor,
        "created_at": utc_isoformat(note.created_at),
    }


async def add_feedback(
    session: AsyncSession,
    *,
    resource_type: str,
    resource_id: int,
    verdict: str,
    corrected_importance: str | None,
    corrected_event_type: str | None,
    comment: str | None,
    actor: str,
) -> dict[str, Any] | None:
    resource = await get_resource(session, resource_type, resource_id)
    if resource is None:
        return None
    feedback = AnalysisFeedback(
        resource_type=resource_type,
        resource_id=resource_id,
        verdict=verdict,
        corrected_importance=corrected_importance,
        corrected_event_type=corrected_event_type,
        comment=comment,
        actor=actor,
        created_at=utcnow(),
    )
    session.add(feedback)
    if corrected_importance:
        if isinstance(resource, Incident):
            resource.top_importance = corrected_importance
        else:
            resource.importance = corrected_importance
    await session.flush()
    add_audit(
        session,
        resource_type,
        resource_id,
        str(getattr(resource, "title", resource_id))[:200],
        "analysis_feedback",
        f"Analysis feedback recorded: {verdict}",
    )
    await session.commit()
    return _feedback_dict(feedback)


def _feedback_dict(feedback: AnalysisFeedback) -> dict[str, Any]:
    return {
        "id": feedback.id,
        "resource_type": feedback.resource_type,
        "resource_id": feedback.resource_id,
        "verdict": feedback.verdict,
        "corrected_importance": feedback.corrected_importance,
        "corrected_event_type": feedback.corrected_event_type,
        "comment": feedback.comment,
        "actor": feedback.actor,
        "created_at": utc_isoformat(feedback.created_at),
    }


async def feedback_summary(session: AsyncSession, *, days: int = 30) -> dict[str, Any]:
    start = utcnow() - timedelta(days=max(1, days))
    rows = (
        await session.execute(
            select(AnalysisFeedback.verdict, func.count(AnalysisFeedback.id))
            .where(AnalysisFeedback.created_at >= start)
            .group_by(AnalysisFeedback.verdict)
        )
    ).all()
    breakdown = {str(verdict): int(count) for verdict, count in rows}
    total = sum(breakdown.values())
    correct = breakdown.get("correct", 0)
    return {
        "window_days": max(1, days),
        "total": total,
        "correct": correct,
        "corrections": total - correct,
        "agreement_pct": round(100.0 * correct / total, 1) if total else None,
        "breakdown": breakdown,
    }


async def merge_incidents(
    session: AsyncSession,
    *,
    destination_id: int,
    source_ids: list[int],
) -> dict[str, Any] | None:
    destination = await session.get(Incident, destination_id)
    if destination is None or destination_id in source_ids:
        return None
    sources = list(
        (
            await session.execute(select(Incident).where(Incident.id.in_(source_ids)).order_by(Incident.id))
        )
        .scalars()
        .all()
    )
    if len(sources) != len(source_ids):
        return None

    await session.execute(
        update(IncidentMember)
        .where(IncidentMember.incident_id.in_(source_ids))
        .values(incident_id=destination_id)
    )
    now = utcnow()
    for source in sources:
        source.status = "closed"
        source.workflow_status = "resolved"
        source.resolved_at = now
        source.ended_at = source.ended_at or now
        source.alert_count = 0
        session.add(
            OperationalNote(
                resource_type="incident",
                resource_id=int(source.id),
                body=f"Merged into incident #{destination_id}",
                actor="system",
                created_at=now,
            )
        )
    await _refresh_incident_aggregate(session, destination)
    add_audit(
        session,
        "incident",
        destination_id,
        destination.title,
        "merged",
        f"Merged incidents into #{destination_id}: {', '.join(str(value) for value in source_ids)}",
    )
    await session.commit()
    return {"destination": destination_id, "merged": source_ids, "alert_count": destination.alert_count}


async def split_incident(
    session: AsyncSession,
    *,
    source_id: int,
    event_ids: list[int],
) -> dict[str, Any] | None:
    source = await session.get(Incident, source_id)
    if source is None:
        return None
    members = list(
        (
            await session.execute(
                select(IncidentMember, WebhookEvent)
                .join(WebhookEvent, WebhookEvent.id == IncidentMember.event_id)
                .where(
                    IncidentMember.incident_id == source_id,
                    IncidentMember.event_id.in_(event_ids),
                )
                .order_by(IncidentMember.event_timestamp, IncidentMember.id)
            )
        ).all()
    )
    if len(members) != len(event_ids) or len(members) >= source.alert_count:
        return None
    first_event = members[0][1]
    from services.incidents.grouping import _correlation_dimensions, _event_rule_name

    rule_name = _event_rule_name(first_event)
    title = f"{first_event.source or 'unknown'} incident"
    if rule_name:
        title = f"{title} — {rule_name}"
    new_incident = Incident(
        title=title,
        status="active",
        workflow_status="open",
        source=first_event.source,
        started_at=first_event.timestamp or utcnow(),
        alert_count=0,
        top_importance=first_event.importance,
        correlation_dimensions=_correlation_dimensions(first_event),
        correlation_confidence=1.0,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    session.add(new_incident)
    await session.flush()
    await session.execute(
        update(IncidentMember)
        .where(
            IncidentMember.incident_id == source_id,
            IncidentMember.event_id.in_(event_ids),
        )
        .values(incident_id=new_incident.id)
    )
    await _refresh_incident_aggregate(session, source)
    await _refresh_incident_aggregate(session, new_incident)
    add_audit(
        session,
        "incident",
        int(new_incident.id),
        new_incident.title,
        "split",
        f"Split {len(event_ids)} alerts from incident #{source_id}",
    )
    await session.commit()
    return {"source": source_id, "created": new_incident.id, "moved_event_ids": event_ids}


async def _refresh_incident_aggregate(session: AsyncSession, incident: Incident) -> None:
    events = list(
        (
            await session.execute(
                select(WebhookEvent)
                .join(IncidentMember, IncidentMember.event_id == WebhookEvent.id)
                .where(IncidentMember.incident_id == incident.id)
                .order_by(WebhookEvent.timestamp, WebhookEvent.id)
            )
        )
        .scalars()
        .all()
    )
    incident.alert_count = len(events)
    if not events:
        return
    incident.started_at = events[0].timestamp
    incident.updated_at = events[-1].timestamp
    incident.source = events[0].source if len({event.source for event in events}) == 1 else "multiple"
    ranks = {"low": 1, "medium": 2, "high": 3}
    incident.top_importance = max(
        (event.importance for event in events if event.importance),
        key=lambda value: ranks.get(str(value), 0),
        default=None,
    )

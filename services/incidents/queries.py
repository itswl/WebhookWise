"""Read-side queries for the incidents dashboard."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from core.datetime_utils import utc_isoformat
from models import Incident, IncidentMember, WebhookEvent
from services.operations.workflow import list_notes
from services.pagination import apply_cursor_window, trim_cursor_window

# Exactly the columns _incident_row serializes (summary_analysis and
# summary_last_error stay deferred; they are detail-view-only).
_INCIDENT_ROW_ATTRS = (
    Incident.id,
    Incident.title,
    Incident.status,
    Incident.source,
    Incident.started_at,
    Incident.ended_at,
    Incident.alert_count,
    Incident.top_importance,
    Incident.workflow_status,
    Incident.assignee,
    Incident.team,
    Incident.acknowledged_at,
    Incident.resolved_at,
    Incident.sla_due_at,
    Incident.correlation_dimensions,
    Incident.correlation_confidence,
    Incident.summary_status,
    Incident.created_at,
)


async def list_incidents(
    session: AsyncSession,
    *,
    cursor: int | None = None,
    status: str = "",
    page: int = 1,
    page_size: int = 20,
    min_alert_count: int = 2,
) -> tuple[list[dict[str, Any]], bool, int | None]:
    """Cursor-paginated incident list, newest first."""
    # The list never renders summary_analysis (LLM-output JSONB) or
    # summary_last_error — defer everything _incident_row doesn't read so the
    # page doesn't drag detail-only blobs along per row.
    query = (
        select(Incident).options(load_only(*_INCIDENT_ROW_ATTRS)).where(Incident.alert_count >= max(1, min_alert_count))
    )
    if status:
        query = query.where(Incident.status == status)
    query = query.order_by(Incident.id.desc())
    query = apply_cursor_window(query, Incident.id, page=page, page_size=page_size, cursor=cursor)

    result = await session.execute(query)
    page_window = trim_cursor_window(list(result.scalars().all()), page_size, lambda i: i.id)
    rows = [_incident_row(i) for i in page_window.rows]
    return rows, page_window.has_more, page_window.next_cursor


async def get_incident_detail(session: AsyncSession, incident_id: int) -> dict[str, Any] | None:
    """Full incident detail with the member alert summaries inline."""
    incident = await session.get(Incident, incident_id)
    if incident is None:
        return None

    result = _incident_row(incident)
    result["summary_analysis"] = incident.summary_analysis or {}
    result["summary_status"] = incident.summary_status
    result["notes"] = await list_notes(session, resource_type="incident", resource_id=incident_id) or []

    # Load only the most recent 50 members through the normalized membership
    # table. The database supplies timeline order and enforces event integrity.
    member_stmt = (
        # Project only what the member card renders: the full entity would drag
        # raw_payload and the whole ai_analysis JSONB along for 50 rows when the
        # card needs one summary string out of it.
        select(
            WebhookEvent.id,
            WebhookEvent.source,
            WebhookEvent.importance,
            WebhookEvent.timestamp,
            WebhookEvent.ai_analysis["summary"].astext.label("summary"),
            WebhookEvent.is_duplicate,
            WebhookEvent.forward_status,
        )
        .join(IncidentMember, IncidentMember.event_id == WebhookEvent.id)
        .where(IncidentMember.incident_id == incident_id)
        .order_by(IncidentMember.event_timestamp.desc(), IncidentMember.id.desc())
        .limit(50)
    )
    members = list((await session.execute(member_stmt)).all())
    members.reverse()
    result["member_ids"] = [int(event.id) for event in members]
    result["members"] = [
        {
            "id": e.id,
            "source": e.source,
            "importance": e.importance,
            "timestamp": utc_isoformat(e.timestamp),
            "summary": str(e.summary or "")[:200],
            "is_duplicate": bool(e.is_duplicate),
            "forward_status": e.forward_status,
        }
        for e in members
    ]

    return result


async def get_incident_summary(session: AsyncSession, incident_id: int) -> dict[str, Any] | None:
    """Return a structured overview of one incident."""
    incident = await session.get(Incident, incident_id)
    if incident is None:
        return None

    result = _incident_row(incident)
    result["summary_analysis"] = incident.summary_analysis or {}
    return result


def _incident_row(incident: Incident) -> dict[str, Any]:
    return {
        "id": incident.id,
        "title": incident.title,
        "status": incident.status,
        "source": incident.source,
        "started_at": utc_isoformat(incident.started_at),
        "ended_at": utc_isoformat(incident.ended_at),
        "alert_count": incident.alert_count,
        "top_importance": incident.top_importance,
        "workflow_status": incident.workflow_status,
        "assignee": incident.assignee,
        "team": incident.team,
        "acknowledged_at": utc_isoformat(incident.acknowledged_at),
        "resolved_at": utc_isoformat(incident.resolved_at),
        "sla_due_at": utc_isoformat(incident.sla_due_at),
        "correlation_dimensions": incident.correlation_dimensions or {},
        "correlation_confidence": incident.correlation_confidence,
        "summary_status": incident.summary_status,
        "created_at": utc_isoformat(incident.created_at),
    }

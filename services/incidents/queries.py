"""Read-side queries for the incidents dashboard."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utc_isoformat, utcnow
from models import Incident, WebhookEvent
from services.pagination import apply_cursor_window, trim_cursor_window


async def list_incidents(
    session: AsyncSession,
    *,
    cursor: int | None = None,
    status: str = "",
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[dict[str, Any]], bool, int | None]:
    """Cursor-paginated incident list, newest first."""
    query = select(Incident)
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

    # Fetch member alert summaries for the timeline.
    member_ids = incident.member_ids or []
    if member_ids:
        # Load the most recent 50 members for the detail view.
        recent_ids = member_ids[-50:]
        members = list(
            (
                await session.execute(
                    select(WebhookEvent).where(WebhookEvent.id.in_(recent_ids))
                )
            ).scalars().all()
        )
        members.sort(key=lambda e: (e.timestamp is not None, getattr(e, "timestamp", utcnow())))
        result["members"] = [
            {
                "id": e.id,
                "source": e.source,
                "importance": e.importance,
                "timestamp": utc_isoformat(e.timestamp),
                "summary": (
                    str(e.ai_analysis.get("summary", "") or "")[:200]
                    if isinstance(e.ai_analysis, dict)
                    else ""
                ),
                "is_duplicate": bool(e.is_duplicate),
                "forward_status": e.forward_status,
            }
            for e in members
        ]
    else:
        result["members"] = []

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
        "member_ids": incident.member_ids or [],
        "created_at": utc_isoformat(incident.created_at),
    }

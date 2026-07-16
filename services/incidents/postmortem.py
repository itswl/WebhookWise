"""One-click postmortem draft: assemble an incident's full story as Markdown.

Everything a postmortem needs is already recorded — member alerts, the
decision-trace outcomes, workflow timestamps (ack / escalation / resolution),
and the AI incident summary. This module is pure assembly over those rows (no
LLM call): the export is a *draft* for a human to edit, with the system's
facts filled in so nobody reconstructs a timeline from chat scrollback.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utc_isoformat, utcnow
from models import DecisionTrace, Incident, IncidentMember, KBDocument, WebhookEvent

_TIMELINE_LIMIT = 40


def _fmt(ts: datetime | None) -> str:
    return utc_isoformat(ts) or "—"


def _duration(start: datetime | None, end: datetime | None) -> str:
    if start is None or end is None:
        return "ongoing"
    minutes = int((end - start).total_seconds() // 60)
    hours, mins = divmod(max(0, minutes), 60)
    return f"{hours}h {mins}m" if hours else f"{mins}m"


def _event_line(event_row: Any, outcome_by_event: dict[int, str]) -> str:
    summary = ""
    analysis = event_row.ai_analysis if isinstance(event_row.ai_analysis, dict) else {}
    if analysis:
        summary = str(analysis.get("summary") or "").strip()
    delivered = outcome_by_event.get(int(event_row.id), "")
    delivery_note = f" · {delivered}" if delivered else ""
    duplicate_note = " · duplicate" if bool(event_row.is_duplicate) else ""
    text = summary or f"{event_row.source} event"
    return (
        f"| {_fmt(event_row.timestamp)} | {event_row.source or '—'} | "
        f"{event_row.importance or '—'} | {text[:120]}{duplicate_note}{delivery_note} |"
    )


async def build_postmortem_markdown(session: AsyncSession, incident_id: int) -> str | None:
    """Render the incident as a Markdown postmortem draft; None if absent."""
    incident = await session.get(Incident, incident_id)
    if incident is None:
        return None

    member_rows = (
        await session.execute(
            select(
                WebhookEvent.id,
                WebhookEvent.timestamp,
                WebhookEvent.source,
                WebhookEvent.importance,
                WebhookEvent.is_duplicate,
                WebhookEvent.ai_analysis,
            )
            .join(IncidentMember, IncidentMember.event_id == WebhookEvent.id)
            .where(IncidentMember.incident_id == incident_id)
            .order_by(IncidentMember.event_timestamp.asc(), IncidentMember.id.asc())
            .limit(_TIMELINE_LIMIT)
        )
    ).all()

    event_ids = [int(row.id) for row in member_rows]
    outcome_by_event: dict[int, str] = {}
    if event_ids:
        trace_rows = (
            await session.execute(
                select(DecisionTrace.webhook_event_id, DecisionTrace.outcome, DecisionTrace.skip_code).where(
                    DecisionTrace.webhook_event_id.in_(event_ids)
                )
            )
        ).all()
        for event_id, outcome, skip_code in trace_rows:
            label = "forwarded" if outcome == "forwarded" else f"skipped ({skip_code})"
            outcome_by_event[int(event_id)] = label

    lines: list[str] = [
        f"# Postmortem draft: {incident.title}",
        "",
        f"- **Incident:** #{incident.id}",
        f"- **Status:** {incident.status} / {incident.workflow_status}",
        f"- **Source(s):** {incident.source or '—'}",
        f"- **Top importance:** {incident.top_importance or '—'}",
        f"- **Started:** {_fmt(incident.started_at)}",
        f"- **Resolved:** {_fmt(incident.resolved_at)}",
        f"- **Duration:** {_duration(incident.started_at, incident.resolved_at)}",
        f"- **Alerts in incident:** {incident.alert_count}",
        f"- **Assignee:** {incident.assignee or 'unassigned'}",
    ]
    if incident.acknowledged_at is not None:
        lines.append(f"- **Acknowledged:** {_fmt(incident.acknowledged_at)}")
    if incident.escalated_at is not None:
        lines.append(f"- **Escalated (SLA breach):** {_fmt(incident.escalated_at)}")

    summary = incident.summary_analysis if isinstance(incident.summary_analysis, dict) else {}
    for heading, key in (
        ("Summary", "summary"),
        ("Root cause", "root_cause"),
        ("Impact", "impact"),
    ):
        value = str(summary.get(key) or "").strip()
        if value:
            lines += ["", f"## {heading}", "", value]

    lines += ["", "## Timeline", ""]
    if member_rows:
        lines += ["| Time (UTC) | Source | Importance | What happened |", "| --- | --- | --- | --- |"]
        lines += [_event_line(row, outcome_by_event) for row in member_rows]
        if incident.alert_count > len(member_rows):
            lines.append("")
            lines.append(f"_Showing the first {len(member_rows)} of {incident.alert_count} member alerts._")
    else:
        lines.append("_No member alerts recorded._")

    # Workflow milestones appended to the timeline as bullet points.
    milestones: list[tuple[datetime | None, str]] = [
        (incident.escalated_at, "SLA-breach escalation notified"),
        (incident.acknowledged_at, "Acknowledged"),
        (incident.resolved_at, "Resolved"),
    ]
    milestone_lines = [f"- {_fmt(ts)} — {label}" for ts, label in milestones if ts is not None]
    if milestone_lines:
        lines += ["", "### Milestones", ""] + milestone_lines

    raw_recommendations = summary.get("recommendations")
    recommendation_items = raw_recommendations if isinstance(raw_recommendations, list) else []
    recommendations = [str(r).strip() for r in recommendation_items if str(r).strip()]
    lines += ["", "## Action items", ""]
    if recommendations:
        lines += [f"- [ ] {r}" for r in recommendations]
    else:
        lines.append("- [ ] _Fill in follow-ups._")

    kb_ref = (
        await session.execute(
            select(KBDocument.title, KBDocument.status)
            .where(KBDocument.source_ref == f"incident:{incident_id}", KBDocument.chunk_index == 0)
            .limit(1)
        )
    ).first()
    if kb_ref is not None:
        lines += ["", f"_Knowledge base: “{kb_ref.title}” ({kb_ref.status})._"]

    lines += ["", f"_Generated by WebhookWise at {utc_isoformat(utcnow())}_"]
    return "\n".join(lines).rstrip() + "\n"

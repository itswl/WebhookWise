"""On-call handoff summary — one-screen overview of recent activity.

The numbers ARE the summary; no AI needed.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utcnow
from models import Incident, WebhookEvent


async def get_handoff_summary(session: AsyncSession, *, hours: int = 8) -> dict[str, Any]:
    """Return a structured handoff summary for the last *hours* hours."""
    hours = max(1, min(int(hours), 72))
    start = utcnow() - timedelta(hours=hours)

    total_alerts = int(
        await session.scalar(select(func.count(WebhookEvent.id)).where(WebhookEvent.timestamp >= start)) or 0
    )
    high_alerts = int(
        await session.scalar(
            select(func.count(WebhookEvent.id)).where(
                WebhookEvent.timestamp >= start, WebhookEvent.importance == "high"
            )
        ) or 0
    )
    top_sources: list[Any] = list(
        (
            await session.execute(
                select(WebhookEvent.source, func.count(WebhookEvent.id))
                .where(WebhookEvent.timestamp >= start)
                .group_by(WebhookEvent.source)
                .order_by(func.count(WebhookEvent.id).desc())
                .limit(5)
            )
        ).all()
    )

    active_incidents: list[Any] = list(
        (
            await session.execute(
                select(Incident.id, Incident.title, Incident.alert_count, Incident.top_importance)
                .where(Incident.started_at >= start, Incident.status == "active")
                .order_by(Incident.alert_count.desc())
                .limit(10)
            )
        ).all()
    )
    quiet_rows: list[Any] = list(
        (
            await session.execute(
                select(Incident.id, Incident.title, Incident.alert_count)
                .where(Incident.started_at >= start, Incident.status == "quiet")
                .order_by(Incident.alert_count.desc())
                .limit(5)
            )
        ).all()
    )

    lines = [f"## On-call Handoff — Last {hours}h\n"]
    lines.append("### Alerts")
    lines.append(f"{total_alerts} total, {high_alerts} high-priority")
    if top_sources:
        source_lines = [f"  · {row[0] or 'unknown'}: {row[1]} alerts" for row in top_sources]
        lines.append("Top sources:")
        lines.extend(source_lines)
    lines.append(f"\n### Active Incidents ({len(active_incidents)})")
    for row in active_incidents:
        imp = row[3] or "?"
        lines.append(f"  🔥 [{imp}] {row[1][:80]} — {row[2]} alerts")
    if not active_incidents:
        lines.append("  ✅ None")
    lines.append(f"\n### Recently Quieted ({len(quiet_rows)})")
    lines.extend(f"  🔇 {row[1][:80]} — {row[2]} alerts" for row in quiet_rows)
    if not quiet_rows:
        lines.append("  — None")

    return {
        "hours": hours,
        "total_alerts": total_alerts,
        "high_alerts": high_alerts,
        "active_incidents": len(active_incidents),
        "quiet_incidents": len(quiet_rows),
        "active_incident_list": [
            {"id": row[0], "title": row[1], "alert_count": row[2], "importance": row[3]}
            for row in active_incidents
        ],
        "top_sources": [{"source": row[0] or "unknown", "count": row[1]} for row in top_sources],
        "summary_text": "\n".join(lines),
    }

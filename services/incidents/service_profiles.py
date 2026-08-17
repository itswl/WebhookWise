"""Read-time service health profiles derived from incident history."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from core.datetime_utils import utc_isoformat, utcnow
from models import ChangeEvent, Incident, KBDocument
from services.incidents.change_impact import assess_change_impact

_MAX_INCIDENTS = 1_000
_MAX_CHANGES = 100
_MAX_RUNBOOKS = 200


def _normalized(value: object) -> str:
    return str(value or "").strip().lower()


def _incident_dimensions(incident: Incident) -> dict[str, str]:
    return {
        str(key): _normalized(value)
        for key, value in (incident.correlation_dimensions or {}).items()
        if _normalized(value)
    }


def _duration_minutes(end: datetime | None, start: datetime) -> float | None:
    if end is None or end < start:
        return None
    return (end - start).total_seconds() / 60


def _average(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 1) if values else None


def _top_values(values: list[str], limit: int = 5) -> list[dict[str, object]]:
    return [
        {"value": value, "count": count}
        for value, count in Counter(value for value in values if value).most_common(limit)
    ]


def _profile_health(
    incidents: list[Incident],
    *,
    average_resolution_minutes: float | None,
) -> tuple[int, str, list[dict[str, object]]]:
    now = utcnow()
    active = sum(incident.workflow_status not in {"resolved", "ignored"} for incident in incidents)
    high_active = sum(
        incident.workflow_status not in {"resolved", "ignored"}
        and str(incident.top_importance or "").lower() in {"high", "critical"}
        for incident in incidents
    )
    sla_breaches = sum(
        incident.sla_due_at is not None
        and incident.sla_due_at <= now
        and incident.workflow_status not in {"resolved", "ignored"}
        for incident in incidents
    )
    deductions: list[dict[str, object]] = []
    if active:
        deductions.append({"code": "active_incidents", "value": active, "points": min(30, active * 10)})
    if high_active:
        deductions.append({"code": "high_active_incidents", "value": high_active, "points": min(20, high_active * 10)})
    if sla_breaches:
        deductions.append({"code": "sla_breaches", "value": sla_breaches, "points": min(30, sla_breaches * 15)})
    if average_resolution_minutes is not None and average_resolution_minutes > 240:
        deductions.append(
            {
                "code": "slow_resolution",
                "value": average_resolution_minutes,
                "points": 10,
            }
        )
    score = max(
        0,
        100 - sum(int(points) for item in deductions if isinstance((points := item.get("points")), int)),
    )
    label = "healthy" if score >= 80 else "watch" if score >= 55 else "at_risk"
    return score, label, deductions


async def _service_incidents(
    session: AsyncSession,
    *,
    service: str,
    environment: str,
    start: datetime,
) -> list[Incident]:
    filters = [
        Incident.started_at >= start,
        Incident.alert_count > 0,
        Incident.correlation_dimensions["service"].as_string() == service,
    ]
    if environment:
        filters.append(Incident.correlation_dimensions["environment"].as_string() == environment)
    return list(
        (
            await session.execute(
                select(Incident)
                .where(*filters)
                .order_by(Incident.started_at.desc(), Incident.id.desc())
                .limit(_MAX_INCIDENTS)
            )
        )
        .scalars()
        .all()
    )


def _runbook_matches(document: KBDocument, service: str, environment: str) -> bool:
    tags = document.tags if isinstance(document.tags, dict) else {}
    tagged_service = _normalized(tags.get("service"))
    services = tags.get("services")
    service_values = {_normalized(value) for value in services} if isinstance(services, list) else set()
    if service not in ({tagged_service} | service_values):
        return False
    tagged_environment = _normalized(tags.get("environment"))
    environments = tags.get("environments")
    environment_values = {_normalized(value) for value in environments} if isinstance(environments, list) else set()
    return (
        not environment
        or not (tagged_environment or environment_values)
        or environment in ({tagged_environment} | environment_values)
    )


async def get_service_profile(
    session: AsyncSession,
    service: str,
    *,
    environment: str = "",
    window_days: int = 30,
    include_change_impact: bool = True,
) -> dict[str, Any] | None:
    """Build one transparent service profile without introducing a CMDB."""
    service_key = _normalized(service)
    environment_key = _normalized(environment)
    if not service_key:
        return None
    window_days = max(1, min(int(window_days), 365))
    start = utcnow() - timedelta(days=window_days)
    incidents = await _service_incidents(
        session,
        service=service_key,
        environment=environment_key,
        start=start,
    )

    change_filters = [
        ChangeEvent.started_at >= start,
        func.lower(ChangeEvent.service) == service_key,
    ]
    if environment_key:
        change_filters.append(func.lower(ChangeEvent.environment) == environment_key)
    change_query = (
        select(ChangeEvent)
        .where(*change_filters)
        .order_by(ChangeEvent.started_at.desc(), ChangeEvent.id.desc())
        .limit(_MAX_CHANGES)
    )
    changes = list((await session.execute(change_query)).scalars().all())

    documents = list(
        (
            await session.execute(
                select(KBDocument)
                .options(
                    load_only(
                        KBDocument.id,
                        KBDocument.title,
                        KBDocument.source_ref,
                        KBDocument.tags,
                        KBDocument.updated_at,
                    )
                )
                .where(
                    KBDocument.status == "published",
                    KBDocument.chunk_index == 0,
                )
                .order_by(KBDocument.updated_at.desc(), KBDocument.id.desc())
                .limit(_MAX_RUNBOOKS)
            )
        )
        .scalars()
        .all()
    )
    runbooks = [document for document in documents if _runbook_matches(document, service_key, environment_key)]
    if not incidents and not changes and not runbooks:
        return None

    acknowledgement_minutes = [
        duration
        for incident in incidents
        if (duration := _duration_minutes(incident.acknowledged_at, incident.started_at)) is not None
    ]
    resolution_minutes = [
        duration
        for incident in incidents
        if (duration := _duration_minutes(incident.resolved_at, incident.started_at)) is not None
    ]
    average_acknowledgement = _average(acknowledgement_minutes)
    average_resolution = _average(resolution_minutes)
    health_score, health_label, health_deductions = _profile_health(
        incidents,
        average_resolution_minutes=average_resolution,
    )
    root_causes = []
    for incident in incidents:
        summary = incident.summary_analysis if isinstance(incident.summary_analysis, dict) else {}
        root_cause = str(summary.get("root_cause") or "").strip()
        if root_cause:
            root_causes.append(root_cause[:300])

    owners = [
        value
        for incident in incidents
        for value in (str(incident.team or "").strip(), str(incident.assignee or "").strip())
        if value
    ]
    resolved_count = sum(incident.workflow_status == "resolved" for incident in incidents)
    active_count = sum(incident.workflow_status not in {"resolved", "ignored"} for incident in incidents)
    high_count = sum(str(incident.top_importance or "").lower() in {"high", "critical"} for incident in incidents)
    environment_values = sorted(
        {
            dimensions["environment"]
            for incident in incidents
            if (dimensions := _incident_dimensions(incident)).get("environment")
        }
        | {_normalized(change.environment) for change in changes if _normalized(change.environment)}
    )
    recent_changes: list[dict[str, object]] = []
    for change in changes[:5]:
        item: dict[str, object] = {
            "id": int(change.id),
            "external_id": change.external_id,
            "change_type": change.change_type,
            "version_from": change.version_from,
            "version_to": change.version_to,
            "status": change.status,
            "started_at": utc_isoformat(change.started_at),
            "source_url": change.source_url,
        }
        if include_change_impact:
            item["impact_assessment"] = await assess_change_impact(session, change)
        recent_changes.append(item)

    return {
        "service": service_key,
        "environment": environment_key or None,
        "environments": environment_values,
        "window_days": window_days,
        "health": {
            "score": health_score,
            "label": health_label,
            "deductions": health_deductions,
        },
        "incident_count": len(incidents),
        "active_incident_count": active_count,
        "high_incident_count": high_count,
        "resolved_incident_count": resolved_count,
        "alert_count": sum(max(0, int(incident.alert_count)) for incident in incidents),
        "acknowledgement_rate_pct": (
            round(100.0 * len(acknowledgement_minutes) / len(incidents), 1) if incidents else None
        ),
        "resolution_rate_pct": (round(100.0 * resolved_count / len(incidents), 1) if incidents else None),
        "average_mtta_minutes": average_acknowledgement,
        "average_mttr_minutes": average_resolution,
        "historical_owners": _top_values(owners, 5),
        "common_incidents": _top_values([incident.title for incident in incidents], 5),
        "common_root_causes": _top_values(root_causes, 5),
        "recent_incidents": [
            {
                "id": int(incident.id),
                "title": incident.title,
                "workflow_status": incident.workflow_status,
                "importance": incident.top_importance,
                "started_at": utc_isoformat(incident.started_at),
                "resolved_at": utc_isoformat(incident.resolved_at),
            }
            for incident in incidents[:5]
        ],
        "change_count": len(changes),
        "recent_changes": recent_changes,
        "runbooks": [
            {
                "document_id": int(document.id),
                "title": document.title,
                "source_ref": document.source_ref,
            }
            for document in runbooks[:5]
        ],
        "generated_at": utc_isoformat(utcnow()),
    }


async def list_service_profiles(
    session: AsyncSession,
    *,
    window_days: int = 30,
    limit: int = 50,
) -> list[dict[str, object]]:
    """List lightweight discovered-service summaries from recent incidents."""
    window_days = max(1, min(int(window_days), 365))
    candidates = list(
        (
            await session.execute(
                select(Incident)
                .where(
                    Incident.started_at >= utcnow() - timedelta(days=window_days),
                    Incident.alert_count > 0,
                )
                .order_by(Incident.started_at.desc(), Incident.id.desc())
                .limit(_MAX_INCIDENTS)
            )
        )
        .scalars()
        .all()
    )
    grouped: dict[str, list[Incident]] = {}
    for incident in candidates:
        service = _incident_dimensions(incident).get("service")
        if service:
            grouped.setdefault(service, []).append(incident)
    rows: list[dict[str, object]] = [
        {
            "service": service,
            "incident_count": len(incidents),
            "active_incident_count": sum(item.workflow_status not in {"resolved", "ignored"} for item in incidents),
            "alert_count": sum(max(0, int(item.alert_count)) for item in incidents),
            "last_seen_at": utc_isoformat(max(item.started_at for item in incidents)),
        }
        for service, incidents in grouped.items()
    ]
    rows.sort(
        key=lambda row: (
            int(str(row["active_incident_count"])),
            int(str(row["incident_count"])),
            str(row["last_seen_at"]),
        ),
        reverse=True,
    )
    return rows[: max(1, min(int(limit), 200))]

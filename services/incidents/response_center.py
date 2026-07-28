"""Bounded read models for incident response work and knowledge gaps."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import median
from typing import Literal, NotRequired, TypedDict

from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from core.datetime_utils import utc_isoformat, utcnow
from models import Incident, KBDocument, RunbookExecution

WorkQueueBucket = Literal["my", "unassigned", "sla_risk", "needs_recovery", "active"]

WORK_QUEUE_BUCKETS: tuple[WorkQueueBucket, ...] = (
    "my",
    "unassigned",
    "sla_risk",
    "needs_recovery",
    "active",
)

_TERMINAL_WORKFLOW_STATUSES = frozenset({"resolved", "ignored"})
_HIGH_IMPORTANCE = frozenset({"critical", "high"})
_KNOWLEDGE_GAP_MAX_INCIDENTS = 2_000
_KNOWLEDGE_GAP_MAX_DOCUMENTS = 500
_KNOWLEDGE_GAP_MAX_EXECUTIONS = 2_000
_FREQUENT_INCIDENT_THRESHOLD = 3
_FREQUENT_ALERT_THRESHOLD = 10
_HIGH_SEVERITY_THRESHOLD = 1
_HIGH_MTTR_MINUTES = 60.0
_ASCII_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_.:/-]{1,63}")
_CJK_RUN_RE = re.compile(r"[\u3400-\u9fff]+")
_GENERIC_KNOWLEDGE_TOKENS = frozenset(
    {
        "alert",
        "critical",
        "error",
        "failure",
        "incident",
        "runbook",
        "service",
        "warning",
    }
)
_TERMINAL_RUNBOOK_STATUSES = frozenset({"completed", "failed", "abandoned"})


class _ScoredReason(TypedDict):
    code: str
    points: float
    value: NotRequired[object]


class _WorkItem(TypedDict):
    incident_id: int
    source_connection_id: int | None
    title: str
    source: str | None
    service: str | None
    environment: str | None
    status: str
    workflow_status: str
    importance: str | None
    assignee: str | None
    team: str | None
    alert_count: int
    started_at: str | None
    updated_at: str | None
    sla_due_at: str | None
    sla_minutes_remaining: float | None
    buckets: list[WorkQueueBucket]
    priority_score: float
    priority_reasons: list[_ScoredReason]
    next_action: dict[str, object]


class _GapItem(TypedDict):
    source_connection_id: int | None
    service: str | None
    environment: str | None
    source: str
    alert_pattern: str
    incident_count: int
    alert_count: int
    high_severity_incident_count: int
    mttr: dict[str, object]
    last_seen: str | None
    knowledge_status: str
    published_runbook_count: int
    runbook_execution_count: int
    ineffective_execution_count: int
    priority_score: float
    priority_reasons: list[_ScoredReason]
    next_action: dict[str, str]


def _normalize(value: object, *, limit: int = 300) -> str:
    return " ".join(str(value or "").strip().lower().split())[:limit]


def _incident_dimensions(incident: Incident) -> dict[str, str]:
    dimensions = incident.correlation_dimensions if isinstance(incident.correlation_dimensions, dict) else {}
    return {str(key): normalized for key, value in dimensions.items() if (normalized := _normalize(value, limit=200))}


def _minutes_until(deadline: datetime | None, now: datetime) -> float | None:
    if deadline is None:
        return None
    return (deadline - now).total_seconds() / 60.0


def _priority_reason(code: str, points: float, value: object | None = None) -> _ScoredReason:
    reason: _ScoredReason = {"code": code, "points": round(points, 1)}
    if value not in (None, ""):
        reason["value"] = value
    return reason


def _work_queue_memberships(
    incident: Incident,
    *,
    actor: str,
    now: datetime,
    sla_risk_minutes: int,
) -> list[WorkQueueBucket]:
    memberships: list[WorkQueueBucket] = ["active"]
    normalized_actor = _normalize(actor, limit=100)
    assignee = _normalize(incident.assignee, limit=100)
    if normalized_actor and assignee == normalized_actor:
        memberships.append("my")
    if not assignee:
        memberships.append("unassigned")
    minutes_until_sla = _minutes_until(incident.sla_due_at, now)
    if minutes_until_sla is not None and minutes_until_sla <= sla_risk_minutes:
        memberships.append("sla_risk")
    if incident.status == "quiet":
        memberships.append("needs_recovery")
    return [bucket for bucket in WORK_QUEUE_BUCKETS if bucket in memberships]


def _next_action(incident: Incident, *, now: datetime) -> dict[str, object]:
    """Return exactly one deterministic next action for an incident."""
    minutes_until_sla = _minutes_until(incident.sla_due_at, now)
    if incident.acknowledged_at is None and (
        _normalize(incident.top_importance) in _HIGH_IMPORTANCE
        or (minutes_until_sla is not None and minutes_until_sla <= 0)
    ):
        return {
            "code": "acknowledge",
            "label": "Acknowledge and begin response",
            "reason": "The incident is high severity or already beyond its SLA without acknowledgement.",
        }
    if not _normalize(incident.assignee, limit=100):
        return {
            "code": "assign_owner",
            "label": "Assign an incident owner",
            "reason": "No operator currently owns the response.",
        }
    if incident.status == "quiet":
        return {
            "code": "confirm_recovery",
            "label": "Confirm recovery or reopen investigation",
            "reason": "Alert activity is quiet, but the incident has not reached a terminal workflow state.",
        }
    if incident.acknowledged_at is None:
        return {
            "code": "acknowledge",
            "label": "Acknowledge the incident",
            "reason": "The incident is still open and has not been acknowledged.",
        }
    if minutes_until_sla is not None and minutes_until_sla <= 0:
        return {
            "code": "escalate_response",
            "label": "Escalate the response",
            "reason": "The incident is still unresolved after its SLA deadline.",
        }
    return {
        "code": "investigate",
        "label": "Continue investigation",
        "reason": "The incident is owned and acknowledged but not yet resolved.",
    }


def _work_item(
    incident: Incident,
    *,
    actor: str,
    now: datetime,
    sla_risk_minutes: int,
) -> _WorkItem:
    reasons: list[_ScoredReason] = []
    importance = _normalize(incident.top_importance)
    severity_points = {
        "critical": 35.0,
        "high": 30.0,
        "medium": 18.0,
        "low": 6.0,
    }.get(importance, 10.0)
    reasons.append(_priority_reason("severity", severity_points, importance or "unknown"))

    minutes_until_sla = _minutes_until(incident.sla_due_at, now)
    if minutes_until_sla is not None:
        if minutes_until_sla <= 0:
            reasons.append(_priority_reason("sla_breached", 35.0, round(abs(minutes_until_sla))))
        elif minutes_until_sla <= 30:
            reasons.append(_priority_reason("sla_due_within_30m", 28.0, round(minutes_until_sla)))
        elif minutes_until_sla <= sla_risk_minutes:
            reasons.append(_priority_reason("sla_at_risk", 18.0, round(minutes_until_sla)))

    if incident.acknowledged_at is None:
        reasons.append(_priority_reason("unacknowledged", 12.0))
    if not _normalize(incident.assignee, limit=100):
        reasons.append(_priority_reason("unassigned", 8.0))
    if incident.status == "quiet":
        reasons.append(_priority_reason("recovery_confirmation_needed", 12.0))

    age_hours = max(0.0, (now - incident.started_at).total_seconds() / 3600.0)
    age_points = min(8.0, age_hours / 3.0)
    if age_points:
        reasons.append(_priority_reason("incident_age", age_points, round(age_hours, 1)))

    dimensions = _incident_dimensions(incident)
    return {
        "incident_id": int(incident.id),
        "source_connection_id": incident.source_connection_id,
        "title": incident.title,
        "source": incident.source,
        "service": dimensions.get("service"),
        "environment": dimensions.get("environment"),
        "status": incident.status,
        "workflow_status": incident.workflow_status,
        "importance": incident.top_importance,
        "assignee": incident.assignee,
        "team": incident.team,
        "alert_count": max(0, int(incident.alert_count)),
        "started_at": utc_isoformat(incident.started_at),
        "updated_at": utc_isoformat(incident.updated_at),
        "sla_due_at": utc_isoformat(incident.sla_due_at),
        "sla_minutes_remaining": round(minutes_until_sla, 1) if minutes_until_sla is not None else None,
        "buckets": _work_queue_memberships(
            incident,
            actor=actor,
            now=now,
            sla_risk_minutes=sla_risk_minutes,
        ),
        "priority_score": round(min(100.0, sum(float(reason["points"]) for reason in reasons)), 1),
        "priority_reasons": reasons,
        "next_action": _next_action(incident, now=now),
    }


async def get_response_work_queue(
    session: AsyncSession,
    *,
    bucket: WorkQueueBucket = "active",
    actor: str = "",
    limit: int = 50,
    offset: int = 0,
    sla_risk_minutes: int = 120,
) -> dict[str, object]:
    """Return one paginated incident-work bucket plus exact bucket counts."""
    limit = max(1, min(int(limit), 100))
    offset = max(0, min(int(offset), 100_000))
    sla_risk_minutes = max(5, min(int(sla_risk_minutes), 24 * 60))
    actor = str(actor or "").strip()[:100]
    if bucket == "my" and not actor:
        raise ValueError("actor is required when bucket is 'my'")

    now = utcnow()
    base_filters = (
        Incident.alert_count > 0,
        Incident.status.in_(["active", "quiet"]),
        Incident.workflow_status.notin_(sorted(_TERMINAL_WORKFLOW_STATUSES)),
    )
    normalized_assignee = func.lower(func.trim(Incident.assignee))
    unassigned_filter = or_(Incident.assignee.is_(None), func.trim(Incident.assignee) == "")
    bucket_filters = {
        "active": (),
        "my": (normalized_assignee == _normalize(actor, limit=100),),
        "unassigned": (unassigned_filter,),
        "sla_risk": (
            Incident.sla_due_at.is_not(None),
            Incident.sla_due_at <= now + timedelta(minutes=sla_risk_minutes),
        ),
        "needs_recovery": (Incident.status == "quiet",),
    }

    counts: dict[WorkQueueBucket, int] = {}
    for candidate_bucket in WORK_QUEUE_BUCKETS:
        count = await session.scalar(
            select(func.count(Incident.id)).where(
                *base_filters,
                *bucket_filters[candidate_bucket],
            )
        )
        counts[candidate_bucket] = int(count or 0)

    # Keep high-severity incidents visible even when many lower-severity items
    # happen to have SLA deadlines. Within each tier, use an explainable score.
    severity_tier = case(
        (func.lower(Incident.top_importance).in_(sorted(_HIGH_IMPORTANCE)), 0),
        (
            Incident.sla_due_at.is_not(None) & (Incident.sla_due_at <= now + timedelta(minutes=sla_risk_minutes)),
            1,
        ),
        (Incident.status == "quiet", 2),
        else_=3,
    )
    severity_points = case(
        (func.lower(Incident.top_importance) == "critical", 35.0),
        (func.lower(Incident.top_importance) == "high", 30.0),
        (func.lower(Incident.top_importance) == "medium", 18.0),
        (func.lower(Incident.top_importance) == "low", 6.0),
        else_=10.0,
    )
    sla_points = case(
        (Incident.sla_due_at <= now, 35.0),
        (Incident.sla_due_at <= now + timedelta(minutes=30), 28.0),
        (Incident.sla_due_at <= now + timedelta(minutes=sla_risk_minutes), 18.0),
        else_=0.0,
    )
    priority_score = (
        severity_points
        + sla_points
        + case((Incident.acknowledged_at.is_(None), 12.0), else_=0.0)
        + case((unassigned_filter, 8.0), else_=0.0)
        + case((Incident.status == "quiet", 12.0), else_=0.0)
    )
    incidents = list(
        (
            await session.execute(
                select(Incident)
                .options(
                    load_only(
                        Incident.id,
                        Incident.source_connection_id,
                        Incident.title,
                        Incident.status,
                        Incident.source,
                        Incident.started_at,
                        Incident.alert_count,
                        Incident.top_importance,
                        Incident.workflow_status,
                        Incident.assignee,
                        Incident.team,
                        Incident.acknowledged_at,
                        Incident.resolved_at,
                        Incident.sla_due_at,
                        Incident.correlation_dimensions,
                        Incident.updated_at,
                    )
                )
                .where(
                    *base_filters,
                    *bucket_filters[bucket],
                )
                .order_by(
                    severity_tier.asc(),
                    priority_score.desc(),
                    Incident.started_at.asc(),
                    Incident.id.asc(),
                )
                .offset(offset)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    items = [
        _work_item(
            incident,
            actor=actor,
            now=now,
            sla_risk_minutes=sla_risk_minutes,
        )
        for incident in incidents
    ]
    total_matches = counts[bucket]
    next_offset = offset + len(items) if offset + len(items) < total_matches else None
    return {
        "bucket": bucket,
        "actor": actor or None,
        "sla_risk_minutes": sla_risk_minutes,
        "offset": offset,
        "next_offset": next_offset,
        "total_matches": total_matches,
        "generated_at": utc_isoformat(now),
        "summary": {
            "counts": counts,
            "returned": len(items),
            "bounded": True,
        },
        "items": items,
        "has_more": next_offset is not None,
    }


def _tokens(value: object) -> set[str]:
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(value or ""))
    normalized = _normalize(text, limit=4_000)
    result = set(_ASCII_TOKEN_RE.findall(normalized))
    for run in _CJK_RUN_RE.findall(normalized):
        if len(run) == 1:
            result.add(run)
        else:
            result.update(run[index : index + 2] for index in range(len(run) - 1))
    return result


def _tag_values(tags: dict[str, object], *keys: str) -> set[str]:
    values: set[str] = set()
    for key in keys:
        raw = tags.get(key)
        if isinstance(raw, list):
            values.update(_normalize(item, limit=200) for item in raw if _normalize(item, limit=200))
        elif normalized := _normalize(raw, limit=200):
            values.add(normalized)
    return values


def _incident_pattern(incident: Incident) -> str:
    title = str(incident.title or "").strip()
    if " — " in title:
        title = title.split(" — ", 1)[1]
    return _normalize(title, limit=200) or "unknown"


@dataclass(slots=True)
class _GapGroup:
    source_connection_id: int | None
    service: str
    environment: str
    source: str
    pattern: str
    incidents: list[Incident] = field(default_factory=list)
    alert_count: int = 0
    high_severity_count: int = 0
    resolution_minutes: list[float] = field(default_factory=list)
    last_seen: datetime | None = None


_GapKey = tuple[int | None, str, str, str, str]


def _group_key(incident: Incident) -> _GapKey:
    dimensions = _incident_dimensions(incident)
    return (
        incident.source_connection_id,
        dimensions.get("service", ""),
        dimensions.get("environment", ""),
        _normalize(incident.source, limit=100) or "unknown",
        _incident_pattern(incident),
    )


def _document_ref(document: KBDocument) -> str:
    return str(document.source_ref or f"kb:{document.id}")


def _document_matches_group(document: KBDocument, group: _GapGroup) -> bool:
    tags = document.tags if isinstance(document.tags, dict) else {}
    if _normalize(tags.get("kind")) != "runbook":
        return False

    tagged_services = _tag_values(tags, "service", "services")
    if tagged_services and group.service not in tagged_services:
        return False
    tagged_environments = _tag_values(tags, "environment", "environments")
    if tagged_environments and group.environment and group.environment not in tagged_environments:
        return False

    tagged_patterns = _tag_values(tags, "alert_pattern", "alert_patterns", "rule", "rules")
    if group.pattern in tagged_patterns:
        return True

    pattern_tokens = _tokens(group.pattern)
    document_tokens = _tokens(f"{document.title} {document.content}")
    scope_tokens = _tokens(f"{group.service} {group.environment} {group.source}")
    specific_overlap = (pattern_tokens & document_tokens) - _GENERIC_KNOWLEDGE_TOKENS - scope_tokens
    if len(specific_overlap) >= 2:
        return True
    return any(len(token) >= 8 for token in specific_overlap)


def _duration_minutes(incident: Incident) -> float | None:
    if incident.resolved_at is None or incident.resolved_at < incident.started_at:
        return None
    return (incident.resolved_at - incident.started_at).total_seconds() / 60.0


def _gap_reason(code: str, points: float, value: object) -> _ScoredReason:
    return {"code": code, "value": value, "points": round(points, 1)}


def _serialize_gap(
    group: _GapGroup,
    *,
    published_refs: set[str],
    executions: list[RunbookExecution],
) -> _GapItem | None:
    terminal_executions = [
        execution
        for execution in executions
        if execution.candidate_ref in published_refs and _normalize(execution.status) in _TERMINAL_RUNBOOK_STATUSES
    ]
    effective_refs = {
        execution.candidate_ref
        for execution in terminal_executions
        if _normalize(execution.status) == "completed" and _normalize(execution.effectiveness) == "effective"
    }
    if effective_refs:
        return None

    negative_executions = sum(
        _normalize(execution.effectiveness) == "ineffective" or _normalize(execution.status) in {"failed", "abandoned"}
        for execution in terminal_executions
    )
    if not published_refs:
        knowledge_status = "missing_runbook"
    elif negative_executions:
        knowledge_status = "ineffective_runbook"
    else:
        knowledge_status = "unproven_runbook"

    incident_count = len(group.incidents)
    average_mttr = (
        round(sum(group.resolution_minutes) / len(group.resolution_minutes), 1) if group.resolution_minutes else None
    )
    p50_mttr = round(float(median(group.resolution_minutes)), 1) if group.resolution_minutes else None
    high_frequency = incident_count >= _FREQUENT_INCIDENT_THRESHOLD or group.alert_count >= _FREQUENT_ALERT_THRESHOLD
    high_severity = group.high_severity_count >= _HIGH_SEVERITY_THRESHOLD
    high_mttr = average_mttr is not None and average_mttr >= _HIGH_MTTR_MINUTES
    if not (high_frequency or high_severity or high_mttr):
        return None

    reasons: list[_ScoredReason] = []
    frequency_points = min(40.0, incident_count * 8.0 + max(0, group.alert_count - incident_count) * 0.8)
    if high_frequency:
        reasons.append(
            _gap_reason(
                "high_frequency",
                frequency_points,
                {"incidents": incident_count, "alerts": group.alert_count},
            )
        )
    severity_points = min(30.0, group.high_severity_count * 12.0)
    if high_severity:
        reasons.append(_gap_reason("high_severity", severity_points, group.high_severity_count))
    mttr_points = min(25.0, float(average_mttr or 0.0) / 12.0)
    if high_mttr:
        reasons.append(_gap_reason("high_mttr", mttr_points, average_mttr))
    knowledge_points = {
        "missing_runbook": 10.0,
        "ineffective_runbook": 8.0,
        "unproven_runbook": 6.0,
    }[knowledge_status]
    reasons.append(_gap_reason(knowledge_status, knowledge_points, len(published_refs)))

    return {
        "source_connection_id": group.source_connection_id,
        "service": group.service or None,
        "environment": group.environment or None,
        "source": group.source,
        "alert_pattern": group.pattern,
        "incident_count": incident_count,
        "alert_count": group.alert_count,
        "high_severity_incident_count": group.high_severity_count,
        "mttr": {
            "average_minutes": average_mttr,
            "p50_minutes": p50_mttr,
            "sample_size": len(group.resolution_minutes),
        },
        "last_seen": utc_isoformat(group.last_seen),
        "knowledge_status": knowledge_status,
        "published_runbook_count": len(published_refs),
        "runbook_execution_count": len(terminal_executions),
        "ineffective_execution_count": negative_executions,
        "priority_score": round(min(100.0, sum(float(reason["points"]) for reason in reasons)), 1),
        "priority_reasons": reasons,
        "next_action": {
            "code": "create_runbook" if not published_refs else "validate_or_improve_runbook",
            "label": "Create a runbook" if not published_refs else "Validate or improve the published runbook",
        },
    }


async def get_knowledge_gaps(
    session: AsyncSession,
    *,
    window_days: int = 90,
    limit: int = 50,
) -> dict[str, object]:
    """Find recurring or costly incident patterns without proven runbook coverage."""
    window_days = max(7, min(int(window_days), 365))
    limit = max(1, min(int(limit), 100))
    incident_limit = min(_KNOWLEDGE_GAP_MAX_INCIDENTS, max(300, limit * 40))
    start = utcnow() - timedelta(days=window_days)
    incidents = list(
        (
            await session.execute(
                select(Incident)
                .options(
                    load_only(
                        Incident.id,
                        Incident.source_connection_id,
                        Incident.title,
                        Incident.status,
                        Incident.source,
                        Incident.started_at,
                        Incident.alert_count,
                        Incident.top_importance,
                        Incident.workflow_status,
                        Incident.resolved_at,
                        Incident.correlation_dimensions,
                        Incident.updated_at,
                    )
                )
                .where(
                    Incident.started_at >= start,
                    Incident.alert_count > 0,
                )
                .order_by(Incident.started_at.desc(), Incident.id.desc())
                .limit(incident_limit)
            )
        )
        .scalars()
        .all()
    )

    groups: dict[_GapKey, _GapGroup] = {}
    incident_groups: dict[int, _GapKey] = {}
    for incident in incidents:
        key = _group_key(incident)
        group = groups.setdefault(
            key,
            _GapGroup(
                source_connection_id=key[0],
                service=key[1],
                environment=key[2],
                source=key[3],
                pattern=key[4],
            ),
        )
        group.incidents.append(incident)
        group.alert_count += max(0, int(incident.alert_count))
        if _normalize(incident.top_importance) in _HIGH_IMPORTANCE:
            group.high_severity_count += 1
        if (duration := _duration_minutes(incident)) is not None:
            group.resolution_minutes.append(duration)
        last_seen = incident.updated_at or incident.started_at
        if group.last_seen is None or last_seen > group.last_seen:
            group.last_seen = last_seen
        incident_groups[int(incident.id)] = key

    documents = list(
        (
            await session.execute(
                select(KBDocument)
                .options(
                    load_only(
                        KBDocument.id,
                        KBDocument.title,
                        KBDocument.source_ref,
                        KBDocument.content,
                        KBDocument.tags,
                    )
                )
                .where(
                    KBDocument.status == "published",
                    KBDocument.chunk_index == 0,
                )
                .order_by(KBDocument.updated_at.desc(), KBDocument.id.desc())
                .limit(_KNOWLEDGE_GAP_MAX_DOCUMENTS)
            )
        )
        .scalars()
        .all()
    )
    documents_by_ref = {_document_ref(document): document for document in documents}

    executions: list[RunbookExecution] = []
    if incident_groups:
        executions = list(
            (
                await session.execute(
                    select(RunbookExecution)
                    .where(RunbookExecution.incident_id.in_(list(incident_groups)))
                    .order_by(RunbookExecution.updated_at.desc(), RunbookExecution.id.desc())
                    .limit(_KNOWLEDGE_GAP_MAX_EXECUTIONS)
                )
            )
            .scalars()
            .all()
        )
    executions_by_group: dict[_GapKey, list[RunbookExecution]] = defaultdict(list)
    for execution in executions:
        execution_key = incident_groups.get(int(execution.incident_id))
        if execution_key is not None:
            executions_by_group[execution_key].append(execution)

    gaps: list[_GapItem] = []
    for key, group in groups.items():
        published_refs = {
            reference for reference, document in documents_by_ref.items() if _document_matches_group(document, group)
        }
        published_refs.update(
            execution.candidate_ref
            for execution in executions_by_group.get(key, [])
            if execution.candidate_ref in documents_by_ref
        )
        serialized = _serialize_gap(
            group,
            published_refs=published_refs,
            executions=executions_by_group.get(key, []),
        )
        if serialized is not None:
            gaps.append(serialized)

    gaps.sort(
        key=lambda item: (
            -float(item["priority_score"]),
            str(item["last_seen"] or ""),
            str(item["service"] or ""),
            str(item["alert_pattern"]),
        )
    )
    incident_scan_truncated = len(incidents) >= incident_limit
    document_scan_truncated = len(documents) >= _KNOWLEDGE_GAP_MAX_DOCUMENTS
    execution_scan_truncated = len(executions) >= _KNOWLEDGE_GAP_MAX_EXECUTIONS
    return {
        "window_days": window_days,
        "generated_at": utc_isoformat(utcnow()),
        "thresholds": {
            "frequent_incidents": _FREQUENT_INCIDENT_THRESHOLD,
            "frequent_alerts": _FREQUENT_ALERT_THRESHOLD,
            "high_severity_incidents": _HIGH_SEVERITY_THRESHOLD,
            "high_mttr_minutes": _HIGH_MTTR_MINUTES,
        },
        "summary": {
            "gap_count": len(gaps),
            "scanned_incidents": len(incidents),
            "incident_limit": incident_limit,
            "scanned_documents": len(documents),
            "document_limit": _KNOWLEDGE_GAP_MAX_DOCUMENTS,
            "scanned_executions": len(executions),
            "execution_limit": _KNOWLEDGE_GAP_MAX_EXECUTIONS,
            "bounded": True,
            "truncated": (incident_scan_truncated or document_scan_truncated or execution_scan_truncated),
            "truncation": {
                "incidents": incident_scan_truncated,
                "documents": document_scan_truncated,
                "executions": execution_scan_truncated,
            },
        },
        "items": gaps[:limit],
        "has_more": (
            len(gaps) > limit or incident_scan_truncated or document_scan_truncated or execution_scan_truncated
        ),
    }

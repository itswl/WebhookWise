"""Explainable incident similarity, change correlation, and runbook ranking."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import naive_utc, utc_isoformat, utcnow
from models import (
    ChangeEvent,
    Incident,
    IncidentIntelligenceFeedback,
    IncidentMember,
    KBDocument,
    WebhookEvent,
)
from services.incidents.grouping import _correlation_dimensions, _event_rule_name
from services.operations.audit_logger import add_audit

_MAX_HISTORICAL_INCIDENTS = 200
_MAX_HISTORICAL_MEMBERS = 2_000
_MAX_KB_DOCUMENTS = 200
_CHANGE_LOOKBACK_HOURS = 6
_CHANGE_FOLLOWUP_MINUTES = 15
_ASCII_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_.:/-]{1,63}")
_CJK_RUN_RE = re.compile(r"[\u3400-\u9fff]+")
_RESOURCE_ALIASES = (
    "resource_id",
    "resourceid",
    "instance_id",
    "instanceid",
    "pod",
    "pod_name",
    "hostname",
    "host",
)
_METRIC_ALIASES = (
    "metric",
    "metric_name",
    "metricname",
    "__name__",
)


@dataclass(slots=True)
class _IncidentProfile:
    source: str = ""
    dimensions: dict[str, str] = field(default_factory=dict)
    rules: set[str] = field(default_factory=set)
    metrics: set[str] = field(default_factory=set)
    resources: set[str] = field(default_factory=set)
    tokens: set[str] = field(default_factory=set)


def _normalize(value: object) -> str:
    return str(value or "").strip().lower()[:500]


def _tokens(value: object) -> set[str]:
    """Tokenize English identifiers and Chinese bigrams for local matching."""
    normalized = _normalize(value)
    result = set(_ASCII_TOKEN_RE.findall(normalized))
    for run in _CJK_RUN_RE.findall(normalized):
        if len(run) == 1:
            result.add(run)
        else:
            result.update(run[index : index + 2] for index in range(len(run) - 1))
    return result


def _flatten(payload: Mapping[str, object]) -> dict[str, object]:
    flattened: dict[str, object] = {}
    queue: list[Mapping[str, object]] = [payload]
    while queue and len(flattened) < 150:
        current = queue.pop(0)
        for key, value in current.items():
            normalized = str(key).replace("-", "_").lower()
            flattened.setdefault(normalized, value)
            if isinstance(value, dict):
                queue.append(value)
            elif isinstance(value, list):
                queue.extend(item for item in value[:5] if isinstance(item, dict))
    return flattened


def _event_values(event: WebhookEvent, aliases: tuple[str, ...]) -> set[str]:
    parsed = event.parsed_data or {}
    if not isinstance(parsed, dict):
        return set()
    flattened = _flatten(parsed)
    return {
        normalized
        for alias in aliases
        if (normalized := _normalize(flattened.get(alias))) and normalized not in {"none", "null"}
    }


def _profile(incident: Incident, events: list[WebhookEvent]) -> _IncidentProfile:
    dimensions = {
        str(key): _normalize(value)
        for key, value in (incident.correlation_dimensions or {}).items()
        if _normalize(value)
    }
    rules: set[str] = set()
    metrics: set[str] = set()
    resources: set[str] = set()
    texts: list[str] = [incident.title]
    if isinstance(incident.summary_analysis, dict):
        texts.extend(str(value) for value in incident.summary_analysis.values())
    for event in events:
        for key, value in _correlation_dimensions(event).items():
            if value:
                dimensions.setdefault(key, value)
        rule = _normalize(_event_rule_name(event))
        if rule:
            rules.add(rule)
        metrics.update(_event_values(event, _METRIC_ALIASES))
        resources.update(_event_values(event, _RESOURCE_ALIASES))
        if isinstance(event.ai_analysis, dict):
            texts.extend(
                str(event.ai_analysis.get(key) or "") for key in ("summary", "event_type", "root_cause", "impact")
            )
    return _IncidentProfile(
        source=_normalize(incident.source),
        dimensions=dimensions,
        rules=rules,
        metrics=metrics,
        resources=resources,
        tokens=_tokens(" ".join(texts)),
    )


def _overlap_score(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / max(1, min(len(left), len(right)))


def _reason(code: str, value: str | int | float | None = None) -> dict[str, object]:
    result: dict[str, object] = {"code": code}
    if value is not None and value != "":
        result["value"] = value
    return result


def _similarity(current: _IncidentProfile, candidate: _IncidentProfile) -> tuple[float, list[dict[str, object]]]:
    score = 0.0
    reasons: list[dict[str, object]] = []
    dimension_weights = {
        "service": 0.25,
        "environment": 0.12,
        "project": 0.12,
        "region": 0.06,
    }
    for dimension, weight in dimension_weights.items():
        value = current.dimensions.get(dimension)
        if value and value == candidate.dimensions.get(dimension):
            score += weight
            reasons.append(_reason(f"same_{dimension}", value))
    for values, candidate_values, weight, code in (
        (current.rules, candidate.rules, 0.20, "same_rule"),
        (current.metrics, candidate.metrics, 0.10, "same_metric"),
        (current.resources, candidate.resources, 0.08, "same_resource"),
    ):
        overlap = values & candidate_values
        if overlap:
            score += weight
            reasons.append(_reason(code, sorted(overlap)[0]))
    if current.source and current.source == candidate.source:
        score += 0.03
        reasons.append(_reason("same_source", current.source))
    text_score = _overlap_score(current.tokens, candidate.tokens)
    if text_score >= 0.1:
        score += min(0.20, text_score * 0.20)
        reasons.append(_reason("similar_description", round(text_score, 2)))
    return min(score, 1.0), reasons


async def _incident_members(
    session: AsyncSession,
    incident_ids: list[int],
    *,
    limit: int,
) -> dict[int, list[WebhookEvent]]:
    if not incident_ids:
        return {}
    rows = (
        await session.execute(
            select(IncidentMember.incident_id, WebhookEvent)
            .join(WebhookEvent, WebhookEvent.id == IncidentMember.event_id)
            .where(IncidentMember.incident_id.in_(incident_ids))
            .order_by(IncidentMember.event_timestamp.desc())
            .limit(limit)
        )
    ).all()
    grouped: dict[int, list[WebhookEvent]] = defaultdict(list)
    for incident_id, event in rows:
        grouped[int(incident_id)].append(event)
    return grouped


def _summary_field(incident: Incident, key: str) -> object:
    summary = incident.summary_analysis or {}
    return summary.get(key) if isinstance(summary, dict) else None


async def _similar_incidents(
    session: AsyncSession,
    incident: Incident,
    current_profile: _IncidentProfile,
    feedback: Mapping[tuple[str, str], str],
    limit: int,
) -> list[dict[str, object]]:
    candidates = list(
        (
            await session.execute(
                select(Incident)
                .where(
                    Incident.id != incident.id,
                    Incident.status.in_(["quiet", "closed"]),
                )
                .order_by(Incident.started_at.desc(), Incident.id.desc())
                .limit(_MAX_HISTORICAL_INCIDENTS)
            )
        )
        .scalars()
        .all()
    )
    members = await _incident_members(
        session,
        [int(candidate.id) for candidate in candidates],
        limit=_MAX_HISTORICAL_MEMBERS,
    )
    ranked: list[tuple[float, dict[str, object]]] = []
    for candidate in candidates:
        score, reasons = _similarity(
            current_profile,
            _profile(candidate, members.get(int(candidate.id), [])),
        )
        if score < 0.20 or not reasons:
            continue
        candidate_ref = f"incident:{candidate.id}"
        ranked.append(
            (
                score,
                {
                    "candidate_ref": candidate_ref,
                    "incident_id": candidate.id,
                    "title": candidate.title,
                    "status": candidate.status,
                    "score": round(score, 3),
                    "reasons": reasons,
                    "started_at": utc_isoformat(candidate.started_at),
                    "resolved_at": utc_isoformat(candidate.resolved_at or candidate.ended_at),
                    "root_cause": _summary_field(candidate, "root_cause"),
                    "resolution": _summary_field(candidate, "recommendations"),
                    "feedback": feedback.get(("similar_incident", candidate_ref)),
                },
            )
        )
    ranked.sort(key=lambda item: (item[0], str(item[1]["started_at"])), reverse=True)
    return [item[1] for item in ranked[:limit]]


def _change_dimensions(change: ChangeEvent) -> dict[str, str]:
    return {
        key: value
        for key, raw in (
            ("service", change.service),
            ("environment", change.environment),
            ("project", change.project),
            ("region", change.region),
        )
        if (value := _normalize(raw))
    }


def _change_score(
    incident: Incident,
    current: _IncidentProfile,
    change: ChangeEvent,
) -> tuple[float, list[dict[str, object]], float]:
    offset_minutes = (incident.started_at - change.started_at).total_seconds() / 60
    score = max(0.0, 1.0 - abs(offset_minutes) / (_CHANGE_LOOKBACK_HOURS * 60)) * 0.20
    time_code = "change_before_incident" if offset_minutes >= 0 else "change_after_incident"
    reasons = [_reason(time_code, abs(round(offset_minutes)))]
    identity_hits = 0
    for dimension, weight in (
        ("service", 0.30),
        ("environment", 0.15),
        ("project", 0.15),
        ("region", 0.08),
    ):
        value = current.dimensions.get(dimension)
        if value and value == _change_dimensions(change).get(dimension):
            score += weight
            identity_hits += 1
            reasons.append(_reason(f"same_{dimension}", value))
    resource = _normalize(change.resource_id)
    if resource and resource in current.resources:
        score += 0.12
        identity_hits += 1
        reasons.append(_reason("same_resource", resource))
    if identity_hits == 0:
        return 0.0, [], offset_minutes
    return min(score, 1.0), reasons, offset_minutes


async def _related_changes(
    session: AsyncSession,
    incident: Incident,
    current_profile: _IncidentProfile,
    feedback: Mapping[tuple[str, str], str],
    limit: int,
) -> list[dict[str, object]]:
    window_end = incident.ended_at or incident.resolved_at or utcnow()
    candidates = list(
        (
            await session.execute(
                select(ChangeEvent)
                .where(
                    ChangeEvent.started_at >= incident.started_at - timedelta(hours=_CHANGE_LOOKBACK_HOURS),
                    ChangeEvent.started_at <= window_end + timedelta(minutes=_CHANGE_FOLLOWUP_MINUTES),
                )
                .order_by(ChangeEvent.started_at.desc(), ChangeEvent.id.desc())
                .limit(200)
            )
        )
        .scalars()
        .all()
    )
    ranked: list[tuple[float, dict[str, object]]] = []
    for change in candidates:
        score, reasons, offset_minutes = _change_score(incident, current_profile, change)
        if score < 0.25:
            continue
        candidate_ref = f"change:{change.id}"
        ranked.append(
            (
                score,
                {
                    "candidate_ref": candidate_ref,
                    "change_id": change.id,
                    "external_id": change.external_id,
                    "source": change.source,
                    "change_type": change.change_type,
                    "service": change.service,
                    "environment": change.environment,
                    "version_from": change.version_from,
                    "version_to": change.version_to,
                    "actor": change.actor,
                    "status": change.status,
                    "started_at": utc_isoformat(change.started_at),
                    "source_url": change.source_url,
                    "score": round(score, 3),
                    "offset_minutes": round(offset_minutes),
                    "reasons": reasons,
                    "feedback": feedback.get(("change", candidate_ref)),
                },
            )
        )
    ranked.sort(key=lambda item: (item[0], str(item[1]["started_at"])), reverse=True)
    return [item[1] for item in ranked[:limit]]


def _tag_values(tags: Mapping[str, object], key: str) -> set[str]:
    raw = tags.get(key)
    if isinstance(raw, list):
        return {_normalize(value) for value in raw if _normalize(value)}
    normalized = _normalize(raw)
    return {normalized} if normalized else set()


def _runbook_score(
    current: _IncidentProfile,
    document: KBDocument,
) -> tuple[float, list[dict[str, object]], str]:
    tags = document.tags if isinstance(document.tags, dict) else {}
    kind = _normalize(tags.get("kind")) or "knowledge"
    score = 0.08 if kind == "runbook" else 0.0
    reasons: list[dict[str, object]] = []
    for dimension, weight in (
        ("service", 0.30),
        ("environment", 0.12),
        ("project", 0.12),
        ("region", 0.06),
    ):
        value = current.dimensions.get(dimension)
        if value and value in (_tag_values(tags, dimension) | _tag_values(tags, f"{dimension}s")):
            score += weight
            reasons.append(_reason(f"same_{dimension}", value))
    source = current.source
    if source and source in _tag_values(tags, "source"):
        score += 0.08
        reasons.append(_reason("same_source", source))
    coverage = _overlap_score(current.tokens, _tokens(f"{document.title} {document.content}"))
    if coverage:
        score += min(0.35, coverage * 0.35)
        reasons.append(_reason("content_match", round(coverage, 2)))
    return min(score, 1.0), reasons, kind


async def _recommended_runbooks(
    session: AsyncSession,
    current_profile: _IncidentProfile,
    feedback: Mapping[tuple[str, str], str],
    limit: int,
) -> list[dict[str, object]]:
    documents = list(
        (
            await session.execute(
                select(KBDocument)
                .where(
                    KBDocument.status == "published",
                    KBDocument.chunk_index == 0,
                )
                .order_by(KBDocument.updated_at.desc(), KBDocument.id.desc())
                .limit(_MAX_KB_DOCUMENTS)
            )
        )
        .scalars()
        .all()
    )
    ranked: list[tuple[float, dict[str, object]]] = []
    for document in documents:
        score, reasons, kind = _runbook_score(current_profile, document)
        if score < 0.10 or not reasons:
            continue
        candidate_ref = document.source_ref or f"kb:{document.id}"
        ranked.append(
            (
                score,
                {
                    "candidate_ref": candidate_ref,
                    "document_id": document.id,
                    "title": document.title,
                    "source_ref": document.source_ref,
                    "source_kind": kind,
                    "score": round(score, 3),
                    "reasons": reasons,
                    "excerpt": document.content.strip()[:360],
                    "feedback": feedback.get(("runbook", candidate_ref)),
                },
            )
        )
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [item[1] for item in ranked[:limit]]


async def _feedback_map(
    session: AsyncSession,
    incident_id: int,
) -> dict[tuple[str, str], str]:
    rows = list(
        (
            await session.execute(
                select(IncidentIntelligenceFeedback).where(IncidentIntelligenceFeedback.incident_id == incident_id)
            )
        )
        .scalars()
        .all()
    )
    return {(row.recommendation_type, row.candidate_ref): row.verdict for row in rows}


async def get_incident_intelligence(
    session: AsyncSession,
    incident_id: int,
    *,
    limit: int = 3,
) -> dict[str, object] | None:
    """Return three bounded, explainable recommendation groups for an incident."""
    incident = await session.get(Incident, incident_id)
    if incident is None:
        return None
    members = await _incident_members(session, [incident_id], limit=100)
    current_profile = _profile(incident, members.get(incident_id, []))
    feedback = await _feedback_map(session, incident_id)
    similar = await _similar_incidents(session, incident, current_profile, feedback, limit)
    changes = await _related_changes(session, incident, current_profile, feedback, limit)
    runbooks = await _recommended_runbooks(session, current_profile, feedback, limit)
    return {
        "incident_id": incident_id,
        "strategy": "deterministic_v1",
        "generated_at": utc_isoformat(utcnow()),
        "similar_incidents": similar,
        "related_changes": changes,
        "recommended_runbooks": runbooks,
    }


async def upsert_change_event(
    session: AsyncSession,
    payload: Mapping[str, Any],
) -> tuple[ChangeEvent, bool]:
    """Insert or update one idempotent normalized change event."""
    source = str(payload["source"])
    external_id = str(payload["external_id"])
    change = (
        await session.execute(
            select(ChangeEvent).where(
                ChangeEvent.source == source,
                ChangeEvent.external_id == external_id,
            )
        )
    ).scalar_one_or_none()
    created = change is None
    if change is None:
        change = ChangeEvent(source=source, external_id=external_id)
        session.add(change)
    for key, value in payload.items():
        if key in {"started_at", "finished_at"} and isinstance(value, datetime):
            value = naive_utc(value)
        setattr(change, key, value)
    await session.flush()
    add_audit(
        session,
        "change_event",
        change.id,
        external_id,
        "ingested" if created else "updated",
        f"Change event {'ingested' if created else 'updated'}: {source}/{external_id}",
        actor=str(payload.get("actor") or "integration"),
    )
    await session.commit()
    await session.refresh(change)
    return change, created


async def record_intelligence_feedback(
    session: AsyncSession,
    incident_id: int,
    payload: Mapping[str, Any],
) -> IncidentIntelligenceFeedback | None:
    """Upsert operator feedback for one recommendation candidate."""
    incident = await session.get(Incident, incident_id)
    if incident is None:
        return None
    recommendation_type = str(payload["recommendation_type"])
    candidate_ref = str(payload["candidate_ref"])
    feedback = (
        await session.execute(
            select(IncidentIntelligenceFeedback).where(
                IncidentIntelligenceFeedback.incident_id == incident_id,
                IncidentIntelligenceFeedback.recommendation_type == recommendation_type,
                IncidentIntelligenceFeedback.candidate_ref == candidate_ref,
            )
        )
    ).scalar_one_or_none()
    if feedback is None:
        feedback = IncidentIntelligenceFeedback(
            incident_id=incident_id,
            recommendation_type=recommendation_type,
            candidate_ref=candidate_ref,
        )
        session.add(feedback)
    feedback.verdict = str(payload["verdict"])
    feedback.comment = payload.get("comment")
    feedback.actor = str(payload.get("actor") or "operator")
    await session.flush()
    add_audit(
        session,
        "incident",
        incident_id,
        incident.title,
        "intel_feedback",
        f"Incident intelligence feedback: {recommendation_type}/{feedback.verdict}",
        actor=feedback.actor,
    )
    await session.commit()
    await session.refresh(feedback)
    return feedback


def change_event_response(change: ChangeEvent) -> dict[str, object]:
    """Serialize a normalized change event for the ingestion response."""
    return {
        "id": change.id,
        "external_id": change.external_id,
        "source": change.source,
        "change_type": change.change_type,
        "started_at": utc_isoformat(change.started_at),
        "updated_at": utc_isoformat(change.updated_at),
    }

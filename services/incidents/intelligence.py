"""Explainable incident similarity, change correlation, and runbook ranking."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from core.datetime_utils import naive_utc, utc_isoformat, utcnow
from models import (
    ChangeEvent,
    Incident,
    IncidentIntelligenceFeedback,
    IncidentMember,
    KBDocument,
    WebhookEvent,
)
from services.incidents.change_impact import assess_change_impact
from services.incidents.grouping import _correlation_dimensions, _event_rule_name
from services.incidents.recommendation_calibration import (
    RecommendationCalibration,
    get_recommendation_calibrations,
)
from services.incidents.runbooks import load_runbook_execution_responses
from services.incidents.service_profiles import get_service_profile
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
    summary = incident.summary_analysis if isinstance(incident.summary_analysis, dict) else {}
    confirmed = incident.resolution_record if isinstance(incident.resolution_record, dict) else {}
    texts.extend(
        str(value)
        for value in (
            summary.get("summary"),
            summary.get("timeline_summary"),
            confirmed.get("root_cause") or summary.get("root_cause"),
            confirmed.get("impact") or summary.get("impact"),
            confirmed.get("resolution"),
            confirmed.get("follow_ups") if "follow_ups" in confirmed else summary.get("recommendations"),
        )
        if value
    )
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
            .options(
                load_only(
                    WebhookEvent.id,
                    WebhookEvent.source,
                    WebhookEvent.parsed_data,
                    WebhookEvent.ai_analysis,
                )
            )
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


def _resolution_field(incident: Incident, key: str) -> object:
    resolution = incident.resolution_record or {}
    return resolution.get(key) if isinstance(resolution, dict) else None


async def _similar_incidents(
    session: AsyncSession,
    incident: Incident,
    current_profile: _IncidentProfile,
    feedback: Mapping[tuple[str, str], str],
    calibration: RecommendationCalibration,
    limit: int,
) -> list[dict[str, object]]:
    historical_filters = [
        Incident.id != incident.id,
        Incident.status.in_(["quiet", "closed"]),
    ]
    service = current_profile.dimensions.get("service", "")
    environment = current_profile.dimensions.get("environment", "")
    if service:
        historical_filters.append(Incident.correlation_dimensions["service"].as_string() == service)
    if environment:
        historical_filters.append(Incident.correlation_dimensions["environment"].as_string() == environment)
    candidates = list(
        (
            await session.execute(
                select(Incident)
                .where(*historical_filters)
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
        raw_score, reasons = _similarity(
            current_profile,
            _profile(candidate, members.get(int(candidate.id), [])),
        )
        score = calibration.apply(raw_score)
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
                    "raw_score": round(raw_score, 3),
                    "calibration": calibration.as_dict(),
                    "reasons": reasons,
                    "started_at": utc_isoformat(candidate.started_at),
                    "resolved_at": utc_isoformat(candidate.resolved_at or candidate.ended_at),
                    "root_cause": _resolution_field(candidate, "root_cause") or _summary_field(candidate, "root_cause"),
                    "resolution": _resolution_field(candidate, "resolution")
                    or (
                        _resolution_field(candidate, "follow_ups")
                        if _resolution_field(candidate, "follow_ups") is not None
                        else _summary_field(candidate, "recommendations")
                    ),
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
    calibration: RecommendationCalibration,
    limit: int,
) -> list[dict[str, object]]:
    window_end = incident.ended_at or incident.resolved_at or utcnow()
    filters = [
        ChangeEvent.started_at >= incident.started_at - timedelta(hours=_CHANGE_LOOKBACK_HOURS),
        ChangeEvent.started_at <= window_end + timedelta(minutes=_CHANGE_FOLLOWUP_MINUTES),
    ]
    service = current_profile.dimensions.get("service")
    project = current_profile.dimensions.get("project")
    if service:
        filters.append(func.lower(ChangeEvent.service) == service)
    elif project:
        filters.append(func.lower(ChangeEvent.project) == project)
    candidates = list(
        (
            await session.execute(
                select(ChangeEvent)
                .where(*filters)
                .order_by(ChangeEvent.started_at.desc(), ChangeEvent.id.desc())
                .limit(200)
            )
        )
        .scalars()
        .all()
    )
    ranked: list[tuple[float, dict[str, object]]] = []
    for change in candidates:
        raw_score, reasons, offset_minutes = _change_score(incident, current_profile, change)
        score = calibration.apply(raw_score)
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
                    "raw_score": round(raw_score, 3),
                    "calibration": calibration.as_dict(),
                    "offset_minutes": round(offset_minutes),
                    "reasons": reasons,
                    "feedback": feedback.get(("change", candidate_ref)),
                },
            )
        )
    ranked.sort(key=lambda item: (item[0], str(item[1]["started_at"])), reverse=True)
    selected = [item[1] for item in ranked[:limit]]
    changes_by_id = {int(change.id): change for change in candidates}
    for item in selected:
        change_id = int(str(item["change_id"]))
        item["impact_assessment"] = await assess_change_impact(
            session,
            changes_by_id[change_id],
        )
    return selected


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
    calibration: RecommendationCalibration,
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
        raw_score, reasons, kind = _runbook_score(current_profile, document)
        score = calibration.apply(raw_score)
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
                    "raw_score": round(raw_score, 3),
                    "calibration": calibration.as_dict(),
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


def _brief(value: object) -> str:
    """Normalize structured summary fragments into one compact display line."""
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        return " ".join(value.split()).strip()[:500]
    if isinstance(value, Mapping):
        for key in ("description", "summary", "cause", "action", "text", "title"):
            if result := _brief(value.get(key)):
                return result
        return ""
    if isinstance(value, list):
        for item in value:
            if result := _brief(item):
                return result
        return ""
    return _brief(str(value))


def _float_or_none(value: object) -> float | None:
    if not isinstance(value, (int, float, str, bytes)):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _command_summary(
    incident: Incident,
    *,
    changes: list[dict[str, object]],
    runbooks: list[dict[str, object]],
    runbook_executions: list[dict[str, object]],
) -> dict[str, object]:
    """Build a deterministic four-part command summary from existing evidence."""
    summary = incident.summary_analysis if isinstance(incident.summary_analysis, dict) else {}
    top_change = changes[0] if changes else {}
    assessment_value = top_change.get("impact_assessment")
    assessment = assessment_value if isinstance(assessment_value, dict) else {}

    likely_cause = _brief(summary.get("root_cause"))
    if not likely_cause and str(assessment.get("level") or "") in {"medium", "high"}:
        likely_cause = _brief(assessment.get("summary"))

    next_action = _brief(summary.get("recommendations"))
    active_execution = next(
        (execution for execution in runbook_executions if str(execution.get("status") or "") == "in_progress"),
        None,
    )
    if active_execution is not None:
        steps_value = active_execution.get("steps")
        steps = steps_value if isinstance(steps_value, list) else []
        next_action = next(
            (_brief(step.get("text")) for step in steps if isinstance(step, dict) and not bool(step.get("completed"))),
            "",
        ) or _brief(active_execution.get("title"))
    elif not next_action and runbooks:
        executed_refs = {str(execution.get("candidate_ref") or "") for execution in runbook_executions}
        next_runbook = next(
            (runbook for runbook in runbooks if str(runbook.get("candidate_ref") or "") not in executed_refs),
            None,
        )
        if next_runbook is not None:
            next_action = _brief(next_runbook.get("title"))

    change_identity = _brief(top_change.get("external_id") or top_change.get("service"))
    change_versions = " → ".join(
        part
        for part in (
            _brief(top_change.get("version_from")),
            _brief(top_change.get("version_to")),
        )
        if part
    )
    recent_change = " · ".join(part for part in (change_identity, change_versions) if part)

    confidence = _float_or_none(summary.get("confidence"))
    if confidence is None and likely_cause and assessment:
        confidence = _float_or_none(assessment.get("confidence"))
    if confidence is not None:
        confidence = max(0.0, min(confidence, 1.0))

    return {
        "what_happened": _brief(summary.get("summary")) or incident.title,
        "likely_cause": likely_cause,
        "impact": _brief(summary.get("impact") or summary.get("impact_scope")),
        "recent_change": recent_change,
        "next_action": next_action,
        "confidence": confidence,
    }


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
    calibrations = await get_recommendation_calibrations(
        session,
        service=current_profile.dimensions.get("service", ""),
        environment=current_profile.dimensions.get("environment", ""),
    )
    similar = await _similar_incidents(
        session,
        incident,
        current_profile,
        feedback,
        calibrations["similar_incident"],
        limit,
    )
    changes = await _related_changes(
        session,
        incident,
        current_profile,
        feedback,
        calibrations["change"],
        limit,
    )
    runbooks = await _recommended_runbooks(
        session,
        current_profile,
        feedback,
        calibrations["runbook"],
        limit,
    )
    runbook_executions = await load_runbook_execution_responses(session, incident_id)
    service = current_profile.dimensions.get("service", "")
    service_profile = (
        await get_service_profile(
            session,
            service,
            environment=current_profile.dimensions.get("environment", ""),
            include_change_impact=False,
        )
        if service
        else None
    )
    return {
        "incident_id": incident_id,
        "strategy": "deterministic_v1",
        "calibration_strategy": "bounded_beta_shrinkage_v1",
        "calibration": {recommendation_type: result.as_dict() for recommendation_type, result in calibrations.items()},
        "generated_at": utc_isoformat(utcnow()),
        "similar_incidents": similar,
        "related_changes": changes,
        "recommended_runbooks": runbooks,
        "runbook_executions": runbook_executions,
        "service_profile": service_profile,
        "command_summary": _command_summary(
            incident,
            changes=changes,
            runbooks=runbooks,
            runbook_executions=runbook_executions,
        ),
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

    def apply_payload(target: ChangeEvent) -> None:
        for key, raw_value in payload.items():
            value = raw_value
            if key in {"started_at", "finished_at"} and isinstance(value, datetime):
                value = naive_utc(value)
            setattr(target, key, value)

    if change is None:
        candidate = ChangeEvent(source=source, external_id=external_id)
        apply_payload(candidate)
        try:
            async with session.begin_nested():
                session.add(candidate)
                await session.flush()
            change = candidate
        except IntegrityError:
            # Another CI worker may have committed the same idempotency key
            # between our read and insert. Re-read and apply the latest payload
            # instead of surfacing a transient 500.
            change = (
                await session.execute(
                    select(ChangeEvent).where(
                        ChangeEvent.source == source,
                        ChangeEvent.external_id == external_id,
                    )
                )
            ).scalar_one_or_none()
            if change is None:
                raise
            created = False
            apply_payload(change)
            await session.flush()
    else:
        apply_payload(change)
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
    verdict = str(payload["verdict"])
    comment = payload.get("comment")
    actor = str(payload.get("actor") or "operator")
    created = False
    feedback = (
        await session.execute(
            select(IncidentIntelligenceFeedback)
            .where(
                IncidentIntelligenceFeedback.incident_id == incident_id,
                IncidentIntelligenceFeedback.recommendation_type == recommendation_type,
                IncidentIntelligenceFeedback.candidate_ref == candidate_ref,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if feedback is None:
        candidate = IncidentIntelligenceFeedback(
            incident_id=incident_id,
            recommendation_type=recommendation_type,
            candidate_ref=candidate_ref,
            verdict=verdict,
            comment=comment,
            actor=actor,
        )
        try:
            async with session.begin_nested():
                session.add(candidate)
                await session.flush()
            feedback = candidate
            created = True
        except IntegrityError:
            feedback = (
                await session.execute(
                    select(IncidentIntelligenceFeedback)
                    .where(
                        IncidentIntelligenceFeedback.incident_id == incident_id,
                        IncidentIntelligenceFeedback.recommendation_type == recommendation_type,
                        IncidentIntelligenceFeedback.candidate_ref == candidate_ref,
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one()

    unchanged = feedback.verdict == verdict and feedback.comment == comment and feedback.actor == actor
    if unchanged and not created:
        await session.commit()
        return feedback

    feedback.verdict = verdict
    feedback.comment = comment
    feedback.actor = actor
    feedback.updated_at = utcnow()
    await session.flush()
    add_audit(
        session,
        "incident",
        incident_id,
        incident.title,
        "intel_feedback",
        f"Incident intelligence feedback: {recommendation_type}/{feedback.verdict}",
        actor=actor,
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

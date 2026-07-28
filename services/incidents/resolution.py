"""Operator-owned incident resolution drafts and completeness reporting."""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utc_isoformat, utcnow
from models.incident import Incident
from models.intelligence import ChangeEvent
from services.operations.audit_logger import add_audit

_RECORD_FIELDS = (
    "root_cause_category",
    "root_cause",
    "resolution",
    "impact",
    "change_association",
    "related_change_id",
    "recovery_evidence",
    "owner",
    "follow_ups",
)
_COMPLETENESS_FIELDS = (
    "root_cause_category",
    "root_cause",
    "resolution",
    "impact",
    "change_association",
    "recovery_evidence",
    "owner",
    "follow_ups",
)


class ResolutionRecordConflictError(ValueError):
    """The proposed resolution record refers to inconsistent data."""


def _normalized_record(value: Mapping[str, object] | None) -> dict[str, object]:
    record = dict(value or {})
    return {key: record[key] for key in _RECORD_FIELDS if key in record}


def _field_is_complete(record: Mapping[str, object], field: str) -> bool:
    if field == "follow_ups":
        # An explicitly saved empty list means the operator confirmed there are
        # no follow-ups; an absent key still means the field was not reviewed.
        return field in record and isinstance(record.get(field), list)
    value = record.get(field)
    return value is not None and bool(str(value).strip())


def resolution_completeness(record: Mapping[str, object] | None) -> dict[str, object]:
    """Return transparent completeness without turning it into a close gate."""
    normalized = _normalized_record(record)
    missing = [field for field in _COMPLETENESS_FIELDS if not _field_is_complete(normalized, field)]
    total = len(_COMPLETENESS_FIELDS)
    completed = total - len(missing)
    return {
        "percent": round(100.0 * completed / total),
        "completed": completed,
        "total": total,
        "missing_fields": missing,
    }


def _response_record(incident: Incident) -> dict[str, object]:
    stored = _normalized_record(incident.resolution_record)
    record: dict[str, object] = {field: stored.get(field) for field in _RECORD_FIELDS}
    if "follow_ups" not in stored:
        record["follow_ups"] = None
    record["actor"] = (
        str(incident.resolution_record.get("actor") or "") if isinstance(incident.resolution_record, dict) else ""
    ) or None
    record["updated_at"] = utc_isoformat(incident.resolution_record_updated_at)
    return record


def resolution_record_response(incident: Incident) -> dict[str, object]:
    """Serialize one draft together with its non-blocking completeness."""
    return {
        "incident_id": int(incident.id),
        "status": "closed" if incident.status == "closed" else "draft",
        "record": _response_record(incident),
        "completeness": resolution_completeness(incident.resolution_record),
    }


async def _validate_change_reference(
    session: AsyncSession,
    record: Mapping[str, object],
) -> None:
    association = str(record.get("change_association") or "")
    raw_change_id = record.get("related_change_id")
    change_id = int(raw_change_id) if isinstance(raw_change_id, int) else None
    if association in {"confirmed", "suspected"} and change_id is None:
        raise ResolutionRecordConflictError("related_change_id is required for a confirmed or suspected change")
    if change_id is not None and association not in {"confirmed", "suspected"}:
        raise ResolutionRecordConflictError("related_change_id is only valid for a confirmed or suspected change")
    if change_id is not None and await session.get(ChangeEvent, change_id) is None:
        raise ResolutionRecordConflictError(f"Change event {change_id} not found")


def _clean_value(field: str, value: object) -> object:
    if field == "follow_ups":
        return list(value) if isinstance(value, list) else None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return value


async def apply_resolution_record(
    session: AsyncSession,
    incident: Incident,
    *,
    changes: Mapping[str, object],
    actor: str,
) -> bool:
    """Apply one partial draft in the caller's transaction.

    Returns whether any durable value changed. Repeating the same request is
    idempotent and does not create another audit row.
    """
    current = _normalized_record(incident.resolution_record)
    updated = dict(current)
    for field, value in changes.items():
        if field in _RECORD_FIELDS:
            updated[field] = _clean_value(field, value)
    await _validate_change_reference(session, updated)

    stored_actor = (
        str(incident.resolution_record.get("actor") or "") if isinstance(incident.resolution_record, dict) else ""
    )
    if updated == current and stored_actor == actor:
        return False

    now = utcnow()
    incident.resolution_record = {**updated, "actor": actor}
    incident.resolution_record_updated_at = now
    incident.updated_at = now
    add_audit(
        session,
        "incident",
        int(incident.id),
        incident.title,
        "resolution_draft",
        f"Incident resolution draft updated: {', '.join(sorted(changes))}",
        actor=actor,
    )
    return True


async def save_resolution_record(
    session: AsyncSession,
    incident_id: int,
    *,
    changes: Mapping[str, object],
    actor: str,
) -> tuple[Incident, bool] | None:
    """Persist a partial resolution draft, returning None for a missing incident."""
    incident = await lock_incident_for_resolution(session, incident_id)
    if incident is None:
        return None
    changed = await apply_resolution_record(
        session,
        incident,
        changes=changes,
        actor=actor,
    )
    await session.commit()
    await session.refresh(incident)
    return incident, changed


async def lock_incident_for_resolution(
    session: AsyncSession,
    incident_id: int,
) -> Incident | None:
    """Load the incident under a row lock before merging its JSON draft."""
    result = await session.execute(
        select(Incident).where(Incident.id == incident_id).with_for_update().execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def get_resolution_record(
    session: AsyncSession,
    incident_id: int,
) -> dict[str, object] | None:
    incident = await session.get(Incident, incident_id)
    return resolution_record_response(incident) if incident is not None else None

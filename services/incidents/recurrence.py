"""Deterministic, reviewable incident recurrence detection."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utc_isoformat, utcnow
from models.incident import Incident, IncidentMember, IncidentRecurrence
from models.webhook import WebhookEvent
from services.incidents.grouping import _event_rule_name, _flatten_payload
from services.operations.audit_logger import add_audit

_LOOKBACK_DAYS = 30
_MAX_CANDIDATES = 100
_MAX_MEMBER_ROWS = 2_000


class RecurrenceNotFoundError(LookupError):
    """No recurrence association exists for this incident."""


class RecurrenceConflictError(ValueError):
    """A recurrence review conflicts with an already terminal decision."""


def _normalized(value: object) -> str:
    return str(value or "").strip().lower()


def _alert_identities(event: WebhookEvent) -> list[str]:
    """Return stable alert identities in descending confidence order."""
    identities: list[str] = []
    rule = _normalized(_event_rule_name(event))
    if not rule and isinstance(event.parsed_data, dict):
        flattened = _flatten_payload(event.parsed_data)
        rule = _normalized(flattened.get("alertname") or flattened.get("rule_name") or flattened.get("rulename"))
    source = _normalized(event.source)
    if event.source_connection_id is not None:
        source = f"{source}#source-connection:{event.source_connection_id}"
    if rule:
        identities.append(f"rule:{source}:{rule}")
    if event.dedup_key:
        identities.append(f"dedup:{source}:{event.dedup_key}")
    if event.alert_hash:
        identities.append(f"hash:{source}:{event.alert_hash}")
    return identities


def _matching_identity(
    current_identities: Iterable[str],
    previous_identities: set[str],
) -> str | None:
    return next((identity for identity in current_identities if identity in previous_identities), None)


async def detect_incident_recurrence(
    session: AsyncSession,
    incident: Incident,
    representative_event: WebhookEvent,
) -> IncidentRecurrence | None:
    """Create at most one pending recurrence for a newly formed incident.

    Both service and environment must match exactly, along with one stable
    alert identity. The most recently resolved matching incident wins.
    """
    if incident.id is None or incident.status != "active" or incident.alert_count < 2:
        return None
    existing = (
        await session.execute(select(IncidentRecurrence).where(IncidentRecurrence.recurring_incident_id == incident.id))
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    dimensions = incident.correlation_dimensions or {}
    service = _normalized(dimensions.get("service"))
    environment = _normalized(dimensions.get("environment"))
    current_identities = _alert_identities(representative_event)
    current_member_events = list(
        (
            await session.execute(
                select(WebhookEvent)
                .join(IncidentMember, IncidentMember.event_id == WebhookEvent.id)
                .where(IncidentMember.incident_id == incident.id)
                .order_by(IncidentMember.event_timestamp.desc(), IncidentMember.id.desc())
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    for event in current_member_events:
        for identity in _alert_identities(event):
            if identity not in current_identities:
                current_identities.append(identity)
    if not service or not environment or not current_identities:
        return None

    started_at = incident.started_at or representative_event.timestamp or utcnow()
    source_scope_filter = (
        Incident.source_connection_id == incident.source_connection_id
        if incident.source_connection_id is not None
        else Incident.source_connection_id.is_(None)
    )
    candidates = list(
        (
            await session.execute(
                select(Incident)
                .where(
                    Incident.id != incident.id,
                    Incident.status == "closed",
                    Incident.workflow_status == "resolved",
                    Incident.alert_count > 0,
                    source_scope_filter,
                    Incident.correlation_dimensions["service"].as_string() == service,
                    Incident.correlation_dimensions["environment"].as_string() == environment,
                    Incident.resolved_at.isnot(None),
                    Incident.resolved_at < started_at,
                    Incident.resolved_at >= started_at - timedelta(days=_LOOKBACK_DAYS),
                )
                .order_by(Incident.resolved_at.desc(), Incident.id.desc())
                .limit(_MAX_CANDIDATES)
            )
        )
        .scalars()
        .all()
    )
    matching_candidates = [
        candidate
        for candidate in candidates
        if _normalized((candidate.correlation_dimensions or {}).get("service")) == service
        and _normalized((candidate.correlation_dimensions or {}).get("environment")) == environment
    ]
    if not matching_candidates:
        return None

    candidate_ids = [int(candidate.id) for candidate in matching_candidates]
    member_rows = (
        await session.execute(
            select(IncidentMember.incident_id, WebhookEvent)
            .join(WebhookEvent, WebhookEvent.id == IncidentMember.event_id)
            .where(IncidentMember.incident_id.in_(candidate_ids))
            .order_by(IncidentMember.event_timestamp.desc(), IncidentMember.id.desc())
            .limit(_MAX_MEMBER_ROWS)
        )
    ).all()
    identities_by_incident: dict[int, set[str]] = defaultdict(set)
    for previous_id, event in member_rows:
        identities_by_incident[int(previous_id)].update(_alert_identities(event))

    previous: Incident | None = None
    matched_identity: str | None = None
    for candidate in matching_candidates:
        matched_identity = _matching_identity(
            current_identities,
            identities_by_incident.get(int(candidate.id), set()),
        )
        if matched_identity is not None:
            previous = candidate
            break
    if previous is None or matched_identity is None:
        return None

    recurrence = IncidentRecurrence(
        previous_incident_id=int(previous.id),
        recurring_incident_id=int(incident.id),
        status="pending",
        match_details={
            "service": service,
            "environment": environment,
            "alert_identity": matched_identity,
            "lookback_days": _LOOKBACK_DAYS,
        },
        detected_at=utcnow(),
    )
    session.add(recurrence)
    add_audit(
        session,
        "incident",
        int(incident.id),
        incident.title,
        "recurrence_pending",
        f"Possible recurrence of incident {previous.id} detected",
        actor="system",
    )
    await session.flush()
    return recurrence


async def _load_recurrence(
    session: AsyncSession,
    incident_id: int,
    *,
    for_update: bool = False,
) -> IncidentRecurrence | None:
    statement = select(IncidentRecurrence).where(IncidentRecurrence.recurring_incident_id == incident_id)
    if for_update:
        statement = statement.with_for_update()
    return (await session.execute(statement)).scalar_one_or_none()


async def recurrence_response(
    session: AsyncSession,
    recurrence: IncidentRecurrence,
) -> dict[str, object]:
    previous = await session.get(Incident, recurrence.previous_incident_id)
    return {
        "recurrence_id": int(recurrence.id),
        "status": recurrence.status,
        "incident_id": int(recurrence.recurring_incident_id),
        "previous_incident": (
            {
                "id": int(previous.id),
                "title": previous.title,
                "resolved_at": utc_isoformat(previous.resolved_at),
            }
            if previous is not None
            else None
        ),
        "match": dict(recurrence.match_details or {}),
        "detected_at": utc_isoformat(recurrence.detected_at),
        "reviewed_at": utc_isoformat(recurrence.reviewed_at),
        "reviewed_by": recurrence.reviewed_by,
        "review_note": recurrence.review_note,
    }


async def get_incident_recurrence(
    session: AsyncSession,
    incident_id: int,
) -> dict[str, object] | None:
    if await session.get(Incident, incident_id) is None:
        return None
    recurrence = await _load_recurrence(session, incident_id)
    if recurrence is None:
        return {
            "incident_id": incident_id,
            "status": None,
            "recurrence": None,
        }
    return await recurrence_response(session, recurrence)


async def review_incident_recurrence(
    session: AsyncSession,
    incident_id: int,
    *,
    decision: str,
    actor: str,
    note: str | None,
) -> tuple[dict[str, object], bool]:
    """Confirm or dismiss a candidate without mutating either incident."""
    if decision not in {"confirmed", "dismissed"}:
        raise RecurrenceConflictError("Unsupported recurrence decision")
    recurrence = await _load_recurrence(session, incident_id, for_update=True)
    if recurrence is None:
        raise RecurrenceNotFoundError(f"No recurrence association found for incident {incident_id}")
    if recurrence.status == decision:
        return await recurrence_response(session, recurrence), False
    if recurrence.status != "pending":
        raise RecurrenceConflictError(f"Recurrence was already {recurrence.status}")

    now = utcnow()
    recurrence.status = decision
    recurrence.reviewed_at = now
    recurrence.reviewed_by = actor
    recurrence.review_note = note
    current = await session.get(Incident, incident_id)
    add_audit(
        session,
        "incident",
        incident_id,
        current.title if current is not None else None,
        f"recurrence_{decision}",
        f"Incident recurrence {decision}: previous incident {recurrence.previous_incident_id}",
        actor=actor,
    )
    await session.commit()
    await session.refresh(recurrence)
    return await recurrence_response(session, recurrence), True

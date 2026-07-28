"""Structured resolution records and reviewable incident recurrences."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.datetime_utils import utcnow


def _response_json(response: Any) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(bytes(response.body)))


class _Scope:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def __aenter__(self) -> AsyncSession:
        return self.session

    async def __aexit__(self, *args: object) -> None:
        if args and args[0] is None:
            await self.session.commit()
        else:
            await self.session.rollback()


def test_resolution_follow_up_items_are_bounded() -> None:
    from pydantic import ValidationError

    from schemas.incident_resolution import IncidentResolutionRequest

    with pytest.raises(ValidationError):
        IncidentResolutionRequest(follow_ups=["x" * 501])


def test_recurrence_identities_are_scoped_to_managed_source_connection() -> None:
    from models import WebhookEvent
    from services.incidents.recurrence import _alert_identities

    first = WebhookEvent(
        source="grafana",
        source_connection_id=101,
        parsed_data={"AlertName": "CheckoutErrors"},
        dedup_key="same-key",
        alert_hash="same-hash",
    )
    second = WebhookEvent(
        source="grafana",
        source_connection_id=202,
        parsed_data={"AlertName": "CheckoutErrors"},
        dedup_key="same-key",
        alert_hash="same-hash",
    )

    assert set(_alert_identities(first)).isdisjoint(_alert_identities(second))


@pytest.mark.asyncio
async def test_close_accepts_optional_resolution_and_remains_idempotent(
    db_session: AsyncSession,
) -> None:
    from api.v1.incidents import close_incident_endpoint
    from models import AuditLog, ChangeEvent, Incident
    from schemas.incident_resolution import IncidentResolutionRequest

    now = utcnow()
    incident = Incident(
        title="checkout unavailable",
        status="active",
        workflow_status="open",
        source="grafana",
        started_at=now,
        alert_count=2,
        summary_analysis={
            "summary": "Generated summary",
            "root_cause": "Generated cause",
            "impact": "Generated impact",
            "recommendations": ["Generated follow-up"],
        },
    )
    change = ChangeEvent(
        external_id="deploy-42",
        source="github-actions",
        change_type="deployment",
        service="checkout",
        environment="prod",
        started_at=now - timedelta(minutes=5),
    )
    db_session.add_all([incident, change])
    await db_session.commit()

    request = IncidentResolutionRequest(
        root_cause_category="deployment",
        root_cause="The release contained an invalid timeout.",
        resolution="Rolled back the release.",
        impact="Checkout failed for twelve minutes.",
        change_association="confirmed",
        related_change_id=int(change.id),
        recovery_evidence="Error rate stayed at baseline for fifteen minutes.",
        owner="alice",
        follow_ups=["Add a deployment guardrail."],
        actor="alice",
    )
    first = await close_incident_endpoint(
        int(incident.id),
        request=request,
        session=db_session,
    )
    first_payload = _response_json(first)
    first_resolved_at = first_payload["data"]["resolved_at"]
    second = await close_incident_endpoint(int(incident.id), session=db_session)
    second_payload = _response_json(second)

    persisted = await db_session.get(Incident, incident.id)
    audit_rows = list(
        (
            await db_session.execute(
                select(AuditLog)
                .where(AuditLog.resource_type == "incident", AuditLog.resource_id == incident.id)
                .order_by(AuditLog.id)
            )
        )
        .scalars()
        .all()
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second_payload["message"] == "incident already closed"
    assert second_payload["data"]["resolved_at"] == first_resolved_at
    assert first_payload["data"]["resolution"]["completeness"] == {
        "percent": 100,
        "completed": 8,
        "total": 8,
        "missing_fields": [],
    }
    assert persisted is not None
    assert persisted.resolution_record["root_cause"] == "The release contained an invalid timeout."
    assert [row.action for row in audit_rows] == ["resolution_draft", "closed"]
    assert [row.actor for row in audit_rows] == ["alice", "alice"]


@pytest.mark.asyncio
async def test_incomplete_resolution_never_blocks_the_legacy_close_call(
    db_session: AsyncSession,
) -> None:
    from api.v1.incidents import close_incident_endpoint
    from models import Incident

    incident = Incident(
        title="legacy close",
        status="active",
        workflow_status="open",
        source="generic_json",
        started_at=utcnow(),
        alert_count=1,
    )
    db_session.add(incident)
    await db_session.commit()

    response = await close_incident_endpoint(int(incident.id), session=db_session)
    payload = _response_json(response)

    assert response.status_code == 200
    assert payload["data"]["status"] == "closed"
    assert payload["data"]["resolution"]["completeness"]["percent"] == 0
    assert len(payload["data"]["resolution"]["completeness"]["missing_fields"]) == 8


@pytest.mark.asyncio
async def test_resolution_draft_is_partial_and_human_facts_override_generated_text(
    db_session: AsyncSession,
) -> None:
    from models import Incident
    from services.incidents.postmortem import build_postmortem_markdown
    from services.incidents.resolution import (
        get_resolution_record,
        save_resolution_record,
    )
    from services.kb.incident_sediment import _compose_kb_content

    incident = Incident(
        title="database saturation",
        status="quiet",
        workflow_status="in_progress",
        source="prometheus",
        started_at=utcnow(),
        alert_count=2,
        summary_analysis={
            "summary": "Generated summary",
            "root_cause": "Incorrect generated cause",
            "impact": "Incorrect generated impact",
            "timeline_summary": "Generated timeline",
            "recommendations": ["Generated action"],
        },
    )
    db_session.add(incident)
    await db_session.commit()

    saved = await save_resolution_record(
        db_session,
        int(incident.id),
        changes={
            "root_cause_category": "capacity",
            "root_cause": "The connection pool was exhausted.",
            "resolution": "Raised the pool limit and drained stuck requests.",
            "impact": "Writes were delayed.",
            "change_association": "ruled_out",
            "recovery_evidence": "Queue depth returned to zero.",
            "owner": "database-team",
            "follow_ups": [],
        },
        actor="bob",
    )
    assert saved is not None
    record = await get_resolution_record(db_session, int(incident.id))
    assert record is not None
    assert record["status"] == "draft"
    completeness = record["completeness"]
    assert isinstance(completeness, dict)
    assert completeness["percent"] == 100

    markdown = await build_postmortem_markdown(db_session, int(incident.id))
    assert markdown is not None
    assert "The connection pool was exhausted." in markdown
    assert "Incorrect generated cause" not in markdown
    assert "Raised the pool limit and drained stuck requests." in markdown
    assert "Generated action" not in markdown

    content = _compose_kb_content(
        incident.summary_analysis or {},
        incident.resolution_record,
    )
    assert "The connection pool was exhausted." in content
    assert "Incorrect generated cause" not in content
    assert "Raised the pool limit and drained stuck requests." in content
    assert "Generated action" not in content


@pytest.mark.asyncio
async def test_recurrence_detection_and_review_are_idempotent(
    db_session: AsyncSession,
) -> None:
    from models import AuditLog, Incident, IncidentMember, WebhookEvent
    from models.incident import IncidentRecurrence
    from services.incidents.queries import list_incidents
    from services.incidents.recurrence import (
        RecurrenceConflictError,
        detect_incident_recurrence,
        review_incident_recurrence,
    )

    now = utcnow()
    previous = Incident(
        title="grafana incident — CheckoutErrors",
        status="closed",
        workflow_status="resolved",
        source="grafana",
        started_at=now - timedelta(days=2),
        resolved_at=now - timedelta(days=2, minutes=-30),
        ended_at=now - timedelta(days=2, minutes=-30),
        alert_count=2,
        correlation_dimensions={"service": "checkout", "environment": "prod"},
    )
    recurring = Incident(
        title="grafana incident — CheckoutErrors",
        status="active",
        workflow_status="open",
        source="grafana",
        started_at=now,
        alert_count=2,
        correlation_dimensions={"service": "checkout", "environment": "prod"},
    )
    previous_event = WebhookEvent(
        source="grafana",
        timestamp=previous.started_at,
        parsed_data={
            "AlertName": "CheckoutErrors",
            "service": "checkout",
            "environment": "prod",
        },
        dedup_key="checkout-errors",
    )
    current_event = WebhookEvent(
        source="grafana",
        timestamp=now,
        parsed_data={
            "AlertName": "CheckoutErrors",
            "service": "checkout",
            "environment": "prod",
        },
        dedup_key="checkout-errors",
    )
    db_session.add_all([previous, recurring, previous_event, current_event])
    await db_session.flush()
    db_session.add(
        IncidentMember(
            incident_id=int(previous.id),
            event_id=int(previous_event.id),
            event_timestamp=previous_event.timestamp,
        )
    )
    await db_session.commit()

    first = await detect_incident_recurrence(db_session, recurring, current_event)
    second = await detect_incident_recurrence(db_session, recurring, current_event)
    await db_session.commit()

    assert first is not None
    assert second is not None
    assert first.id == second.id
    assert first.status == "pending"
    assert first.previous_incident_id == previous.id
    assert first.match_details["service"] == "checkout"

    confirmed, changed = await review_incident_recurrence(
        db_session,
        int(recurring.id),
        decision="confirmed",
        actor="alice",
        note="Same alert after the service was redeployed.",
    )
    repeated, repeated_changed = await review_incident_recurrence(
        db_session,
        int(recurring.id),
        decision="confirmed",
        actor="alice",
        note="This repeated request must be inert.",
    )

    assert changed is True
    assert repeated_changed is False
    assert confirmed["status"] == repeated["status"] == "confirmed"
    with pytest.raises(RecurrenceConflictError, match="already confirmed"):
        await review_incident_recurrence(
            db_session,
            int(recurring.id),
            decision="dismissed",
            actor="bob",
            note=None,
        )

    await db_session.refresh(previous)
    await db_session.refresh(recurring)
    count = int(await db_session.scalar(select(func.count(IncidentRecurrence.id))) or 0)
    audits = list(
        (
            await db_session.execute(
                select(AuditLog.action).where(AuditLog.resource_id == recurring.id).order_by(AuditLog.id)
            )
        )
        .scalars()
        .all()
    )
    assert count == 1
    assert previous.status == "closed"
    assert recurring.status == "active"
    assert audits == ["recurrence_pending", "recurrence_confirmed"]
    incident_rows, _has_more, _cursor = await list_incidents(
        db_session,
        page_size=10,
    )
    recurring_row = next(row for row in incident_rows if row["id"] == recurring.id)
    assert recurring_row["recurrence_candidate"] == {
        "recurrence_id": first.id,
        "status": "confirmed",
        "previous_incident_id": previous.id,
    }


@pytest.mark.asyncio
async def test_recurrence_requires_exact_service_environment_and_alert_identity(
    db_session: AsyncSession,
) -> None:
    from models import Incident, IncidentMember, WebhookEvent
    from services.incidents.recurrence import (
        detect_incident_recurrence,
        get_incident_recurrence,
    )

    now = utcnow()
    previous = Incident(
        title="grafana incident — DiskFull",
        status="closed",
        workflow_status="resolved",
        source="grafana",
        started_at=now - timedelta(days=1),
        resolved_at=now - timedelta(hours=20),
        alert_count=2,
        correlation_dimensions={"service": "storage", "environment": "prod"},
    )
    current = Incident(
        title="grafana incident — DiskFull",
        status="active",
        workflow_status="open",
        source="grafana",
        started_at=now,
        alert_count=2,
        correlation_dimensions={"service": "storage", "environment": "stage"},
    )
    old_event = WebhookEvent(
        source="grafana",
        timestamp=previous.started_at,
        parsed_data={"AlertName": "DiskFull"},
    )
    new_event = WebhookEvent(
        source="grafana",
        timestamp=now,
        parsed_data={"AlertName": "DiskFull"},
    )
    db_session.add_all([previous, current, old_event, new_event])
    await db_session.flush()
    db_session.add(
        IncidentMember(
            incident_id=int(previous.id),
            event_id=int(old_event.id),
            event_timestamp=old_event.timestamp,
        )
    )
    await db_session.commit()

    assert await detect_incident_recurrence(db_session, current, new_event) is None
    response = await get_incident_recurrence(db_session, int(current.id))
    assert response is not None and response["recurrence"] is None


@pytest.mark.asyncio
async def test_recurrence_scopes_fallback_identities_to_the_event_source(
    db_session: AsyncSession,
) -> None:
    from models import Incident, IncidentMember, WebhookEvent
    from services.incidents.recurrence import detect_incident_recurrence

    now = utcnow()
    previous = Incident(
        title="generic incident",
        status="closed",
        workflow_status="resolved",
        source="prometheus",
        started_at=now - timedelta(days=1),
        resolved_at=now - timedelta(hours=20),
        alert_count=2,
        correlation_dimensions={"service": "checkout", "environment": "prod"},
    )
    current = Incident(
        title="generic incident",
        status="active",
        workflow_status="open",
        source="grafana",
        started_at=now,
        alert_count=2,
        correlation_dimensions={"service": "checkout", "environment": "prod"},
    )
    previous_event = WebhookEvent(
        source="prometheus",
        timestamp=previous.started_at,
        parsed_data={"message": "timeout"},
        dedup_key="shared-upstream-key",
        alert_hash="shared-alert-hash",
    )
    current_event = WebhookEvent(
        source="grafana",
        timestamp=now,
        parsed_data={"message": "timeout"},
        dedup_key="shared-upstream-key",
        alert_hash="shared-alert-hash",
    )
    db_session.add_all([previous, current, previous_event, current_event])
    await db_session.flush()
    db_session.add(
        IncidentMember(
            incident_id=int(previous.id),
            event_id=int(previous_event.id),
            event_timestamp=previous_event.timestamp,
        )
    )
    await db_session.commit()

    assert await detect_incident_recurrence(db_session, current, current_event) is None


@pytest.mark.asyncio
async def test_recurrence_filters_dimensions_before_applying_candidate_bound(
    db_session: AsyncSession,
) -> None:
    from models import Incident, IncidentMember, WebhookEvent
    from services.incidents.recurrence import detect_incident_recurrence

    now = utcnow()
    previous = Incident(
        title="grafana incident — CheckoutErrors",
        status="closed",
        workflow_status="resolved",
        source="grafana",
        started_at=now - timedelta(days=10),
        resolved_at=now - timedelta(days=9),
        alert_count=2,
        correlation_dimensions={"service": "checkout", "environment": "prod"},
    )
    current = Incident(
        title="grafana incident — CheckoutErrors",
        status="active",
        workflow_status="open",
        source="grafana",
        started_at=now,
        alert_count=2,
        correlation_dimensions={"service": "checkout", "environment": "prod"},
    )
    previous_event = WebhookEvent(
        source="grafana",
        timestamp=previous.started_at,
        parsed_data={"AlertName": "CheckoutErrors"},
    )
    current_event = WebhookEvent(
        source="grafana",
        timestamp=now,
        parsed_data={"AlertName": "CheckoutErrors"},
    )
    unrelated = [
        Incident(
            title=f"busy unrelated incident {index}",
            status="closed",
            workflow_status="resolved",
            source="grafana",
            started_at=now - timedelta(days=2, minutes=index),
            resolved_at=now - timedelta(days=1, minutes=index),
            alert_count=2,
            correlation_dimensions={
                "service": f"unrelated-{index}",
                "environment": "prod",
            },
        )
        for index in range(101)
    ]
    db_session.add_all([previous, current, previous_event, current_event, *unrelated])
    await db_session.flush()
    db_session.add(
        IncidentMember(
            incident_id=int(previous.id),
            event_id=int(previous_event.id),
            event_timestamp=previous_event.timestamp,
        )
    )
    await db_session.commit()

    recurrence = await detect_incident_recurrence(db_session, current, current_event)

    assert recurrence is not None
    assert recurrence.previous_incident_id == previous.id


@pytest.mark.asyncio
async def test_grouping_marks_a_new_recurrence_pending_without_reopening_history(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from models import Incident, IncidentMember, WebhookEvent
    from models.incident import IncidentRecurrence
    from services.incidents.grouping import run_incident_grouping

    now = utcnow()
    async with db_session_factory.begin() as session:
        previous = Incident(
            title="grafana incident — CheckoutErrors",
            status="closed",
            workflow_status="resolved",
            source="grafana",
            started_at=now - timedelta(days=1),
            resolved_at=now - timedelta(hours=20),
            ended_at=now - timedelta(hours=20),
            alert_count=2,
            correlation_dimensions={"service": "checkout", "environment": "prod"},
        )
        previous_event = WebhookEvent(
            source="grafana",
            timestamp=previous.started_at,
            parsed_data={
                "AlertName": "CheckoutErrors",
                "service": "checkout",
                "environment": "prod",
            },
        )
        first = WebhookEvent(
            source="grafana",
            timestamp=now,
            parsed_data={
                "AlertName": "CheckoutErrors",
                "service": "checkout",
                "environment": "prod",
            },
        )
        second = WebhookEvent(
            source="grafana",
            timestamp=now + timedelta(seconds=1),
            parsed_data={
                "AlertName": "CheckoutErrors",
                "service": "checkout",
                "environment": "prod",
            },
        )
        session.add_all([previous, previous_event, first, second])
        await session.flush()
        session.add(
            IncidentMember(
                incident_id=int(previous.id),
                event_id=int(previous_event.id),
                event_timestamp=previous_event.timestamp,
            )
        )
        previous_id = int(previous.id)

    async with db_session_factory() as session:
        with (
            patch(
                "services.incidents.grouping.session_scope",
                return_value=_Scope(session),
            ),
            patch(
                "services.incidents.summary.run_pending_incident_summaries",
                new=AsyncMock(return_value={"claimed": 0, "completed": 0, "failed": 0}),
            ),
        ):
            stats = await run_incident_grouping()

    async with db_session_factory() as session:
        recurrence = (await session.execute(select(IncidentRecurrence))).scalar_one()
        loaded_previous = await session.get(Incident, previous_id)
        loaded_current = await session.get(Incident, recurrence.recurring_incident_id)

    assert stats["created"] == 1
    assert recurrence.status == "pending"
    assert recurrence.previous_incident_id == previous_id
    assert loaded_previous is not None and loaded_previous.status == "closed"
    assert loaded_current is not None and loaded_current.status == "active"

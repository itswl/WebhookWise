"""Incident-intelligence ranking, change ingestion, and feedback contracts."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utcnow


async def _add_member(
    session: AsyncSession,
    incident_id: int,
    *,
    source: str,
    rule: str,
    service: str,
    metric: str,
    summary: str,
) -> None:
    from models import IncidentMember, WebhookEvent

    event = WebhookEvent(
        source=source,
        timestamp=utcnow(),
        parsed_data={
            "RuleName": rule,
            "service": service,
            "environment": "prod",
            "project": "store",
            "metric_name": metric,
        },
        ai_analysis={"summary": summary},
    )
    session.add(event)
    await session.flush()
    session.add(
        IncidentMember(
            incident_id=incident_id,
            event_id=event.id,
            event_timestamp=event.timestamp,
        )
    )


@pytest.mark.asyncio
async def test_intelligence_ranks_matching_history_changes_and_published_runbooks(
    db_session: AsyncSession,
) -> None:
    from models import ChangeEvent, Incident, KBDocument
    from services.incidents.intelligence import get_incident_intelligence

    now = utcnow()
    current = Incident(
        title="checkout request latency",
        status="active",
        source="grafana",
        started_at=now,
        alert_count=3,
        correlation_dimensions={
            "service": "checkout",
            "environment": "prod",
            "project": "store",
        },
    )
    matching = Incident(
        title="checkout latency after database saturation",
        status="closed",
        source="grafana",
        started_at=now - timedelta(days=7),
        ended_at=now - timedelta(days=7, minutes=-30),
        alert_count=5,
        correlation_dimensions={
            "service": "checkout",
            "environment": "prod",
            "project": "store",
        },
        summary_analysis={
            "root_cause": "Database connection pool saturation",
            "recommendations": ["Roll back the checkout release"],
        },
    )
    unrelated = Incident(
        title="billing queue depth",
        status="closed",
        source="prometheus",
        started_at=now - timedelta(days=3),
        alert_count=2,
        correlation_dimensions={"service": "billing", "environment": "test"},
    )
    db_session.add_all([current, matching, unrelated])
    await db_session.flush()
    await _add_member(
        db_session,
        int(current.id),
        source="grafana",
        rule="checkout-latency",
        service="checkout",
        metric="http_request_duration",
        summary="Checkout requests are timing out",
    )
    await _add_member(
        db_session,
        int(matching.id),
        source="grafana",
        rule="checkout-latency",
        service="checkout",
        metric="http_request_duration",
        summary="Checkout latency increased",
    )
    await _add_member(
        db_session,
        int(unrelated.id),
        source="prometheus",
        rule="queue-depth",
        service="billing",
        metric="queue_depth",
        summary="Billing queue is growing",
    )
    db_session.add_all(
        [
            ChangeEvent(
                external_id="deploy-42",
                source="github-actions",
                change_type="deployment",
                service="checkout",
                environment="prod",
                project="store",
                version_from="v41",
                version_to="v42",
                started_at=now - timedelta(minutes=20),
            ),
            ChangeEvent(
                external_id="deploy-billing",
                source="github-actions",
                change_type="deployment",
                service="billing",
                environment="test",
                project="finance",
                started_at=now - timedelta(minutes=10),
            ),
            KBDocument(
                title="Checkout latency rollback",
                source_ref="wiki:checkout-rollback",
                chunk_index=0,
                content="Rollback checkout when request latency increases after a deployment.",
                content_hash="a" * 64,
                tags={"kind": "runbook", "service": "checkout", "environment": "prod"},
                status="published",
            ),
            KBDocument(
                title="Unreviewed checkout draft",
                source_ref="incident:999",
                chunk_index=0,
                content="Checkout emergency workaround.",
                content_hash="b" * 64,
                tags={"kind": "incident_resolution", "service": "checkout"},
                status="draft",
            ),
        ]
    )
    await db_session.commit()

    result = await get_incident_intelligence(db_session, int(current.id))

    assert result is not None
    assert result["strategy"] == "deterministic_v1"
    assert result["similar_incidents"][0]["incident_id"] == matching.id
    assert all(item["incident_id"] != unrelated.id for item in result["similar_incidents"])
    assert [item["external_id"] for item in result["related_changes"]] == ["deploy-42"]
    assert [item["source_ref"] for item in result["recommended_runbooks"]] == ["wiki:checkout-rollback"]
    assert result["similar_incidents"][0]["reasons"]
    assert result["related_changes"][0]["reasons"]
    assert result["recommended_runbooks"][0]["reasons"]


@pytest.mark.asyncio
async def test_change_ingestion_is_idempotent_and_feedback_is_returned(
    db_session: AsyncSession,
) -> None:
    from models import ChangeEvent, Incident
    from services.incidents.intelligence import (
        get_incident_intelligence,
        record_intelligence_feedback,
        upsert_change_event,
    )

    now = utcnow()
    incident = Incident(
        title="api latency",
        status="active",
        source="grafana",
        started_at=now,
        alert_count=2,
        correlation_dimensions={"service": "api", "environment": "prod"},
    )
    db_session.add(incident)
    await db_session.commit()

    payload = {
        "external_id": "deploy-1",
        "source": "gitlab",
        "change_type": "deployment",
        "service": "api",
        "environment": "prod",
        "started_at": now - timedelta(minutes=5),
        "version_to": "v1",
        "details": {},
    }
    first, first_created = await upsert_change_event(db_session, payload)
    payload["version_to"] = "v2"
    second, second_created = await upsert_change_event(db_session, payload)
    feedback = await record_intelligence_feedback(
        db_session,
        int(incident.id),
        {
            "recommendation_type": "change",
            "candidate_ref": f"change:{second.id}",
            "verdict": "relevant",
            "actor": "tester",
            "comment": "Confirmed by deploy timeline",
        },
    )
    result = await get_incident_intelligence(db_session, int(incident.id))
    count = await db_session.scalar(select(func.count(ChangeEvent.id)))

    assert first_created is True
    assert second_created is False
    assert first.id == second.id
    assert second.version_to == "v2"
    assert count == 1
    assert feedback is not None
    assert result is not None
    assert result["related_changes"][0]["feedback"] == "relevant"

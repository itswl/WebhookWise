"""Response-center queue, knowledge-gap, and calibration boundaries."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utcnow

if TYPE_CHECKING:
    from models import Incident, SourceConnection


def _connection(*, public_id: str, name: str) -> SourceConnection:
    from models import SourceConnection

    return SourceConnection(
        public_id=public_id,
        name=name,
        source_type="grafana",
        token_hash=public_id.ljust(64, "0")[:64],
        token_hint=public_id[-6:],
    )


def _incident(
    *,
    title: str,
    now: datetime,
    importance: str = "low",
    source_connection_id: int | None = None,
    service: str = "checkout",
    sla_due_at: datetime | None = None,
) -> Incident:
    from models import Incident

    return Incident(
        title=title,
        status="active",
        source="grafana",
        source_connection_id=source_connection_id,
        started_at=now,
        alert_count=1,
        top_importance=importance,
        workflow_status="open",
        sla_due_at=sla_due_at,
        correlation_dimensions={"service": service, "environment": "prod"},
    )


@pytest.mark.asyncio
async def test_work_queue_paginates_exactly_and_keeps_no_sla_critical_visible(
    db_session: AsyncSession,
) -> None:
    from services.incidents.response_center import get_response_work_queue

    now = utcnow()
    connection = _connection(public_id="src_queue", name="Queue source")
    db_session.add(connection)
    await db_session.flush()

    critical = _incident(
        title="Critical checkout outage",
        now=now - timedelta(minutes=1),
        importance="critical",
        source_connection_id=int(connection.id),
    )
    lower_priority = [
        _incident(
            title=f"Low severity SLA item {index}",
            now=now - timedelta(minutes=index + 2),
            importance="low",
            source_connection_id=int(connection.id),
            sla_due_at=now - timedelta(minutes=1),
        )
        for index in range(510)
    ]
    db_session.add_all([critical, *lower_priority])
    await db_session.flush()

    first_page = await get_response_work_queue(db_session, limit=1, offset=0)
    second_page = await get_response_work_queue(db_session, limit=1, offset=1)

    assert first_page["total_matches"] == 511
    assert first_page["offset"] == 0
    assert first_page["next_offset"] == 1
    assert first_page["has_more"] is True
    first_items = cast(list[dict[str, object]], first_page["items"])
    first_item = first_items[0]
    assert first_item["incident_id"] == critical.id
    assert first_item["source_connection_id"] == connection.id

    assert second_page["offset"] == 1
    assert second_page["next_offset"] == 2
    assert second_page["total_matches"] == 511
    second_items = cast(list[dict[str, object]], second_page["items"])
    assert second_items[0]["incident_id"] != critical.id


@pytest.mark.asyncio
async def test_knowledge_gaps_isolate_connections_and_require_terminal_evidence(
    db_session: AsyncSession,
) -> None:
    from models import KBDocument, RunbookExecution
    from services.incidents.response_center import get_knowledge_gaps

    now = utcnow()
    first_connection = _connection(public_id="src_first", name="First source")
    second_connection = _connection(public_id="src_second", name="Second source")
    third_connection = _connection(public_id="src_third", name="Third source")
    fourth_connection = _connection(public_id="src_fourth", name="Fourth source")
    db_session.add_all([first_connection, second_connection, third_connection, fourth_connection])
    await db_session.flush()

    first = _incident(
        title="Checkout database deadlock",
        now=now,
        importance="critical",
        source_connection_id=int(first_connection.id),
    )
    second = _incident(
        title="Checkout database deadlock",
        now=now - timedelta(minutes=1),
        importance="critical",
        source_connection_id=int(second_connection.id),
    )
    common_only = _incident(
        title="Service error",
        now=now - timedelta(minutes=2),
        importance="critical",
        source_connection_id=int(third_connection.id),
        service="generic",
    )
    service_tag_only = _incident(
        title="Checkout queue backlog",
        now=now - timedelta(minutes=3),
        importance="critical",
        source_connection_id=int(fourth_connection.id),
    )
    db_session.add_all([first, second, common_only, service_tag_only])
    await db_session.flush()

    specific_ref = "wiki:checkout-deadlock"
    db_session.add_all(
        [
            KBDocument(
                title="Service error runbook",
                source_ref="wiki:generic-error",
                chunk_index=0,
                content="Handle a service error alert incident.",
                content_hash="1" * 64,
                tags={"kind": "runbook"},
                status="published",
            ),
            KBDocument(
                title="Checkout database deadlock recovery",
                source_ref=specific_ref,
                chunk_index=0,
                content="Recover checkout from a database deadlock.",
                content_hash="2" * 64,
                tags={"kind": "runbook"},
                status="published",
            ),
            KBDocument(
                title="Checkout deployment rollback",
                source_ref="wiki:checkout-deployment",
                chunk_index=0,
                content="Roll back a failed checkout deployment.",
                content_hash="3" * 64,
                tags={"kind": "runbook", "service": "checkout"},
                status="published",
            ),
            RunbookExecution(
                incident_id=int(first.id),
                candidate_ref=specific_ref,
                title="Checkout database deadlock recovery",
                status="in_progress",
                effectiveness="effective",
                steps=[],
            ),
            RunbookExecution(
                incident_id=int(second.id),
                candidate_ref=specific_ref,
                title="Checkout database deadlock recovery",
                status="completed",
                effectiveness="effective",
                steps=[],
                completed_at=now,
            ),
        ]
    )
    await db_session.flush()

    result = await get_knowledge_gaps(db_session)
    items = cast(list[dict[str, object]], result["items"])

    assert {(item["source_connection_id"], item["knowledge_status"]) for item in items} == {
        (first_connection.id, "unproven_runbook"),
        (third_connection.id, "missing_runbook"),
        (fourth_connection.id, "missing_runbook"),
    }
    first_item = next(item for item in items if item["source_connection_id"] == first_connection.id)
    assert first_item["runbook_execution_count"] == 0
    assert second_connection.id not in {item["source_connection_id"] for item in items}


@pytest.mark.asyncio
async def test_calibration_filters_scope_before_limits_and_uses_terminal_outcomes(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from models import IncidentIntelligenceFeedback, RunbookExecution
    from services.incidents import recommendation_calibration

    now = utcnow()
    target_incidents = [
        _incident(
            title=f"Checkout target {index}",
            now=now - timedelta(hours=2, minutes=index),
            service="checkout",
        )
        for index in range(4)
    ]
    other_incidents = [
        _incident(
            title=f"Billing noise {index}",
            now=now - timedelta(minutes=index),
            service="billing",
        )
        for index in range(4)
    ]
    db_session.add_all([*target_incidents, *other_incidents])
    await db_session.flush()

    rows: list[object] = []
    for index, incident in enumerate(target_incidents[:2]):
        rows.append(
            IncidentIntelligenceFeedback(
                incident_id=int(incident.id),
                recommendation_type="change",
                candidate_ref=f"change:target:{index}",
                verdict="relevant",
                actor="tester",
                updated_at=now - timedelta(hours=1),
            )
        )
    for index, incident in enumerate(other_incidents):
        rows.append(
            IncidentIntelligenceFeedback(
                incident_id=int(incident.id),
                recommendation_type="change",
                candidate_ref=f"change:noise:{index}",
                verdict="irrelevant",
                actor="tester",
                updated_at=now,
            )
        )

    in_progress_incident = target_incidents[2]
    terminal_incident = target_incidents[3]
    rows.extend(
        [
            IncidentIntelligenceFeedback(
                incident_id=int(in_progress_incident.id),
                recommendation_type="runbook",
                candidate_ref="wiki:still-running",
                verdict="used",
                actor="tester",
            ),
            RunbookExecution(
                incident_id=int(in_progress_incident.id),
                candidate_ref="wiki:still-running",
                title="Still running",
                status="in_progress",
                effectiveness="effective",
                steps=[],
            ),
            IncidentIntelligenceFeedback(
                incident_id=int(terminal_incident.id),
                recommendation_type="runbook",
                candidate_ref="wiki:failed",
                verdict="used",
                actor="tester",
            ),
            RunbookExecution(
                incident_id=int(terminal_incident.id),
                candidate_ref="wiki:failed",
                title="Failed runbook",
                status="failed",
                effectiveness="ineffective",
                steps=[],
            ),
        ]
    )
    db_session.add_all(rows)
    await db_session.flush()

    monkeypatch.setattr(recommendation_calibration, "_MAX_FEEDBACK_ROWS", 4)
    calibrations = await recommendation_calibration.get_recommendation_calibrations(
        db_session,
        service="checkout",
        environment="prod",
    )

    assert calibrations["change"].positive_samples == 2
    assert calibrations["change"].negative_samples == 0
    assert calibrations["runbook"].positive_samples == 1
    assert calibrations["runbook"].negative_samples == 1
    assert calibrations["runbook"].feedback_samples == 1
    assert calibrations["runbook"].execution_samples == 1


def test_calibration_is_neutral_below_threshold_and_bounded() -> None:
    from services.incidents.recommendation_calibration import RecommendationCalibration

    sparse = RecommendationCalibration(
        recommendation_type="change",
        service="checkout",
        environment="prod",
        positive_samples=4,
    )
    positive = RecommendationCalibration(
        recommendation_type="change",
        service="checkout",
        environment="prod",
        positive_samples=10_000,
    )
    negative = RecommendationCalibration(
        recommendation_type="change",
        service="checkout",
        environment="prod",
        negative_samples=10_000,
    )

    assert sparse.adjustment == 0.0
    assert 0.0 < positive.adjustment <= 0.1
    assert -0.1 <= negative.adjustment < 0.0
    assert positive.apply(0.95) == 1.0
    assert negative.apply(0.05) == 0.0

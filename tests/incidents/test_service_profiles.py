from __future__ import annotations

import hashlib
from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utcnow
from models import ChangeEvent, Incident, KBDocument
from services.incidents.service_profiles import get_service_profile, list_service_profiles


@pytest.mark.asyncio
async def test_service_profile_aggregates_response_health_and_runbooks(
    db_session: AsyncSession,
) -> None:
    now = utcnow()
    resolved = Incident(
        title="Checkout latency",
        source="grafana",
        status="closed",
        workflow_status="resolved",
        started_at=now - timedelta(days=2),
        acknowledged_at=now - timedelta(days=2) + timedelta(minutes=5),
        resolved_at=now - timedelta(days=2) + timedelta(minutes=30),
        ended_at=now - timedelta(days=2) + timedelta(minutes=30),
        alert_count=4,
        top_importance="high",
        team="payments",
        assignee="alice",
        correlation_dimensions={"service": "checkout", "environment": "prod"},
        summary_analysis={"root_cause": "Database connection pool exhaustion"},
    )
    active = Incident(
        title="Checkout errors",
        source="prometheus",
        status="active",
        workflow_status="open",
        started_at=now - timedelta(hours=1),
        alert_count=3,
        top_importance="medium",
        team="payments",
        correlation_dimensions={"service": "checkout", "environment": "prod"},
    )
    merged_shell = Incident(
        title="Merged shell",
        source="grafana",
        status="closed",
        workflow_status="resolved",
        started_at=now - timedelta(days=1),
        alert_count=0,
        correlation_dimensions={"service": "checkout", "environment": "prod"},
    )
    unrelated = Incident(
        title="Search error",
        source="grafana",
        status="active",
        workflow_status="open",
        started_at=now - timedelta(hours=2),
        alert_count=9,
        correlation_dimensions={"service": "search", "environment": "prod"},
    )
    change = ChangeEvent(
        source="github",
        external_id="checkout-v2",
        change_type="deployment",
        service="checkout",
        environment="prod",
        version_from="v1",
        version_to="v2",
        started_at=now - timedelta(hours=3),
    )
    content = "- Check database pool saturation\n- Roll back the latest deployment"
    runbook = KBDocument(
        title="Checkout recovery",
        source_ref="wiki://checkout-recovery",
        chunk_index=0,
        content=content,
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
        tags={"kind": "runbook", "service": "checkout", "environment": "prod"},
        status="published",
    )
    db_session.add_all([resolved, active, merged_shell, unrelated, change, runbook])
    await db_session.commit()

    profile = await get_service_profile(
        db_session,
        "checkout",
        environment="prod",
        window_days=30,
    )

    assert profile is not None
    assert profile["incident_count"] == 2
    assert profile["alert_count"] == 7
    assert profile["active_incident_count"] == 1
    assert profile["average_mtta_minutes"] == 5.0
    assert profile["average_mttr_minutes"] == 30.0
    assert profile["historical_owners"][0]["value"] == "payments"
    assert profile["common_root_causes"][0]["value"] == "Database connection pool exhaustion"
    assert profile["recent_changes"][0]["external_id"] == "checkout-v2"
    assert profile["runbooks"][0]["title"] == "Checkout recovery"
    assert profile["health"]["score"] < 100

    rows = await list_service_profiles(db_session, window_days=30)
    assert {row["service"] for row in rows} == {"checkout", "search"}


@pytest.mark.asyncio
async def test_service_profile_requires_discovered_data(
    db_session: AsyncSession,
) -> None:
    assert await get_service_profile(db_session, "missing") is None


@pytest.mark.asyncio
async def test_global_response_metrics_aggregate_all_incidents(
    db_session: AsyncSession,
) -> None:
    """The Overview facade's MTTA/MTTR: same arithmetic as the per-service
    profile, ungrouped — and honest Nones on an empty window instead of
    zeroes pretending to be measurements."""
    from datetime import timedelta

    from core.datetime_utils import utcnow
    from models import Incident
    from services.incidents.service_profiles import global_response_metrics

    empty = await global_response_metrics(db_session, window_days=30)
    assert empty["incident_count"] == 0
    assert empty["average_mtta_minutes"] is None
    assert empty["average_mttr_minutes"] is None
    assert empty["acknowledgement_rate_pct"] is None

    now = utcnow()
    acked = Incident(
        title="a",
        status="closed",
        workflow_status="resolved",
        source="grafana",
        started_at=now - timedelta(minutes=60),
        acknowledged_at=now - timedelta(minutes=50),
        resolved_at=now - timedelta(minutes=30),
        alert_count=2,
    )
    silent = Incident(
        title="b",
        status="active",
        workflow_status="open",
        source="grafana",
        started_at=now - timedelta(minutes=20),
        alert_count=1,
    )
    db_session.add_all([acked, silent])
    await db_session.commit()

    metrics = await global_response_metrics(db_session, window_days=30)
    assert metrics["incident_count"] == 2
    assert metrics["resolved_incident_count"] == 1
    assert metrics["average_mtta_minutes"] == 10.0
    assert metrics["average_mttr_minutes"] == 30.0
    assert metrics["acknowledgement_rate_pct"] == 50.0

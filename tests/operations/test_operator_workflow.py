"""Operator workflow, feedback, incident editing, and integration catalog tests."""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from core.datetime_utils import utcnow
from db.session import Base
from models import AnalysisFeedback, ForwardRule, Incident, IncidentMember, OperationalNote, WebhookEvent


@pytest.fixture()
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db_session:
        yield db_session
    await engine.dispose()


@pytest.mark.asyncio
async def test_alert_workflow_notes_and_feedback_form_a_quality_loop(session: AsyncSession) -> None:
    from services.operations.workflow import add_feedback, add_note, feedback_summary, update_workflow

    event = WebhookEvent(source="prometheus", timestamp=utcnow(), importance="low")
    session.add(event)
    await session.commit()

    workflow = await update_workflow(
        session,
        resource_type="webhook_event",
        resource_id=int(event.id),
        changes={"workflow_status": "acknowledged", "assignee": "alice", "team": "sre", "sla_minutes": 30},
    )
    assert workflow is not None
    assert workflow["workflow_status"] == "acknowledged"
    assert workflow["assignee"] == "alice"
    assert workflow["sla_due_at"] is not None

    note = await add_note(
        session,
        resource_type="webhook_event",
        resource_id=int(event.id),
        body="Investigating the affected service",
        actor="alice",
    )
    feedback = await add_feedback(
        session,
        resource_type="webhook_event",
        resource_id=int(event.id),
        verdict="severity_too_low",
        corrected_importance="high",
        corrected_event_type="service_outage",
        comment="Customer traffic is affected",
        actor="alice",
    )
    assert note and note["actor"] == "alice"
    assert feedback and feedback["corrected_importance"] == "high"
    persisted = await session.get(WebhookEvent, event.id)
    assert persisted is not None and persisted.importance == "high"
    assert len((await session.execute(select(OperationalNote))).scalars().all()) == 1
    assert len((await session.execute(select(AnalysisFeedback))).scalars().all()) == 1
    summary = await feedback_summary(session, days=30)
    assert summary["total"] == 1
    assert summary["corrections"] == 1


@pytest.mark.asyncio
async def test_incidents_can_be_merged_then_split_without_duplicate_membership(session: AsyncSession) -> None:
    from services.operations.workflow import merge_incidents, split_incident

    now = utcnow()
    events = [
        WebhookEvent(source="prometheus", timestamp=now, parsed_data={"RuleName": f"alert-{index}"})
        for index in range(3)
    ]
    session.add_all(events)
    await session.flush()
    destination = Incident(title="destination", status="active", source="prometheus", started_at=now, alert_count=2)
    source = Incident(title="source", status="active", source="prometheus", started_at=now, alert_count=1)
    session.add_all([destination, source])
    await session.flush()
    session.add_all(
        [
            IncidentMember(incident_id=destination.id, event_id=events[0].id, event_timestamp=now),
            IncidentMember(incident_id=destination.id, event_id=events[1].id, event_timestamp=now),
            IncidentMember(incident_id=source.id, event_id=events[2].id, event_timestamp=now),
        ]
    )
    await session.commit()

    merged = await merge_incidents(
        session,
        destination_id=int(destination.id),
        source_ids=[int(source.id)],
    )
    assert merged and merged["alert_count"] == 3
    split = await split_incident(
        session,
        source_id=int(destination.id),
        event_ids=[int(events[2].id)],
    )
    assert split and split["created"] != destination.id
    memberships = list((await session.execute(select(IncidentMember))).scalars().all())
    assert len(memberships) == 3
    assert len({member.event_id for member in memberships}) == 3


@pytest.mark.asyncio
async def test_integration_catalog_installs_openclaw_as_a_forward_rule(session: AsyncSession) -> None:
    from core.app_context import get_config_manager
    from schemas.operations import IntegrationSetupRequest
    from services.operations.integration_catalog import install_integration, integration_catalog

    assert {item["id"] for item in integration_catalog()} == {"feishu", "generic_webhook", "openclaw"}
    get_config_manager().openclaw.OPENCLAW_ENABLED = True
    result = await install_integration(
        session,
        IntegrationSetupRequest(
            template_id="openclaw",
            name="High priority deep analysis",
            importance="high",
        ),
    )
    rule = await session.get(ForwardRule, result["rule_id"])
    assert rule is not None
    assert rule.target_type == "openclaw"
    assert rule.match_importance == "high"


@pytest.mark.asyncio
async def test_action_center_rule_remediation_returns_an_undo_command(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services.operations.remediation import run_remediation

    rule = ForwardRule(name="disabled", target_type="feishu", target_url="https://example.com", enabled=False)
    session.add(rule)
    await session.commit()

    async def healthy_test(**_: object) -> dict[str, str]:
        return {"status": "success"}

    monkeypatch.setattr("services.forwarding.remote.send_forward_rule_test", healthy_test)
    result = await run_remediation(
        session,
        action="test_enable_rule",
        resource_id=int(rule.id),
        resource_type=None,
        batch_size=10,
    )
    assert result["changed"] is True
    assert result["undo"] == {"action": "disable_rule", "resource_id": rule.id}
    persisted = await session.get(ForwardRule, rule.id)
    assert persisted is not None and persisted.enabled is True

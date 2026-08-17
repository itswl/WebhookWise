"""Operator workflow, feedback, incident editing, and integration catalog tests."""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utcnow
from models import AnalysisFeedback, ForwardRule, Incident, IncidentMember, OperationalNote, WebhookEvent


@pytest.fixture
def session(db_session):
    return db_session


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
async def test_resolving_an_incident_queues_one_recap_card(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, temp_config: Any
) -> None:
    """The recap closes the loop in chat: exactly one card per incident (a
    reopen + second resolve reuses the idempotency key), only for "resolved"
    (an "ignored" incident was judged not worth attention), and only when the
    operator opted in."""
    from sqlalchemy import select as sa_select

    from core.datetime_utils import utcnow
    from models import ForwardOutbox
    from services.operations.workflow import update_workflow

    monkeypatch.setattr(temp_config.notifications, "INCIDENT_RESOLVE_RECAP_ENABLED", True)
    monkeypatch.setattr(temp_config.notifications, "DEEP_ANALYSIS_FEISHU_WEBHOOK", "https://open.feishu.cn/hook/x")

    now = utcnow()
    resolved = Incident(
        title="支付网关5xx激增",
        status="active",
        source="grafana",
        started_at=now,
        alert_count=3,
        summary_analysis={"summary": "网关升级触发连接池耗尽", "root_cause": "连接池上限过低"},
    )
    ignored = Incident(title="noise", status="active", source="grafana", started_at=now, alert_count=1)
    session.add_all([resolved, ignored])
    await session.commit()

    await update_workflow(
        session,
        resource_type="incident",
        resource_id=int(resolved.id),
        changes={"workflow_status": "resolved"},
        actor="adrian",
    )
    rows = list(
        (await session.execute(sa_select(ForwardOutbox).where(ForwardOutbox.event_type == "incident_resolved")))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    card = str(rows[0].formatted_payload)
    assert "事件已解决" in card and "网关升级触发连接池耗尽" in card and "adrian" in card

    # Reopen + resolve again: the idempotency key already exists, no second card.
    await update_workflow(
        session, resource_type="incident", resource_id=int(resolved.id), changes={"workflow_status": "open"}
    )
    await update_workflow(
        session, resource_type="incident", resource_id=int(resolved.id), changes={"workflow_status": "resolved"}
    )
    # Ignored incidents never recap.
    await update_workflow(
        session, resource_type="incident", resource_id=int(ignored.id), changes={"workflow_status": "ignored"}
    )
    rows = list(
        (await session.execute(sa_select(ForwardOutbox).where(ForwardOutbox.event_type == "incident_resolved")))
        .scalars()
        .all()
    )
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_recap_stays_silent_when_disabled(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, temp_config: Any
) -> None:
    from sqlalchemy import select as sa_select

    from core.datetime_utils import utcnow
    from models import ForwardOutbox
    from services.operations.workflow import update_workflow

    monkeypatch.setattr(temp_config.notifications, "INCIDENT_RESOLVE_RECAP_ENABLED", False)
    incident = Incident(title="t", status="active", source="s", started_at=utcnow(), alert_count=2)
    session.add(incident)
    await session.commit()

    await update_workflow(
        session, resource_type="incident", resource_id=int(incident.id), changes={"workflow_status": "resolved"}
    )
    rows = (
        (await session.execute(sa_select(ForwardOutbox).where(ForwardOutbox.event_type == "incident_resolved")))
        .scalars()
        .all()
    )
    assert list(rows) == []


@pytest.mark.asyncio
async def test_integration_catalog_installs_openclaw_as_a_forward_rule(session: AsyncSession) -> None:
    from core.app_context import get_config_manager
    from schemas.operations import IntegrationSetupRequest
    from services.operations.integration_catalog import install_integration, integration_catalog

    # Every implemented delivery channel is installable from the catalog —
    # dingtalk/wecom/feishu_relay used to be reachable only by hand-writing a
    # rule despite being advertised as first-class targets.
    assert {item["id"] for item in integration_catalog()} == {
        "feishu",
        "dingtalk",
        "wecom",
        "generic_webhook",
        "feishu_relay",
        "deep_analysis",
    }
    assert all(item.get("sprite") for item in integration_catalog()), "catalog icons come from the sprite sheet"
    get_config_manager().deep_analysis.DEEP_ANALYSIS_ENABLED = True
    result = await install_integration(
        session,
        IntegrationSetupRequest(
            template_id="deep_analysis",
            name="High priority deep analysis",
            importance="high",
        ),
    )
    rule = await session.get(ForwardRule, result["rule_id"])
    assert rule is not None
    assert rule.target_type == "deep_analysis"
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


@pytest.mark.asyncio
async def test_action_center_replay_preserves_managed_source_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.operations.remediation import _enqueue_event

    enqueued: list[dict[str, object]] = []

    async def load_payload(_event: object) -> tuple[dict[str, object], str]:
        return {}, '{"alertname":"CheckoutErrors"}'

    async def enqueue(**kwargs: object) -> None:
        enqueued.append(dict(kwargs))

    monkeypatch.setattr("services.webhooks.repository.load_event_payload", load_payload)
    monkeypatch.setattr("services.operations.tasks.process_webhook_task.kiq", enqueue)
    event = SimpleNamespace(
        source="grafana",
        source_connection_id=42,
        headers={},
        raw_payload=None,
        client_ip="203.0.113.10",
        request_id="managed-replay",
        timestamp=datetime(2026, 7, 26, tzinfo=UTC),
        retry_count=1,
    )

    await _enqueue_event(event)

    assert enqueued == [
        {
            "source_name": "grafana",
            "source_connection_id": 42,
            "raw_headers": {},
            "raw_body": '{"alertname":"CheckoutErrors"}',
            "client_ip": "203.0.113.10",
            "request_id": "managed-replay",
            "received_at": "2026-07-26T00:00:00Z",
            "ingest_retry_count": 1,
        }
    ]

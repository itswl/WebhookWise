from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.datetime_utils import utcnow


@pytest.fixture
def session_factory(db_session_factory):
    return db_session_factory


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


@pytest.mark.asyncio
async def test_grouping_closes_quiet_incident_when_no_new_events(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from models import Incident
    from services.incidents.grouping import run_incident_grouping

    now = utcnow()
    async with session_factory.begin() as session:
        incident = Incident(
            title="quiet",
            status="active",
            source="prometheus",
            started_at=now - timedelta(hours=1),
            updated_at=now - timedelta(minutes=30),
            alert_count=1,
        )
        session.add(incident)
        await session.flush()
        incident_id = incident.id

    async with session_factory() as session:
        with (
            patch("services.incidents.grouping.session_scope", return_value=_Scope(session)),
            patch(
                "services.incidents.summary.run_pending_incident_summaries",
                new=AsyncMock(return_value={"claimed": 0, "completed": 0, "failed": 0}),
            ),
        ):
            stats = await run_incident_grouping()

    async with session_factory() as session:
        persisted = await session.get(Incident, incident_id)
    assert stats == {"scanned": 0, "created": 0, "updated": 0, "closed": 1}
    assert persisted is not None
    assert persisted.status == "quiet"
    assert persisted.summary_status == "skipped"


@pytest.mark.asyncio
async def test_full_incident_rolls_over_to_new_incident(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from models import Incident, IncidentMember, WebhookEvent
    from services.incidents.grouping import _MAX_MEMBERS_PER_INCIDENT, run_incident_grouping

    now = utcnow()
    async with session_factory.begin() as session:
        full = Incident(
            title="prometheus incident — cpu",
            status="active",
            source="prometheus",
            started_at=now,
            updated_at=now,
            alert_count=_MAX_MEMBERS_PER_INCIDENT,
        )
        first = WebhookEvent(
            source="prometheus",
            timestamp=now,
            parsed_data={"RuleName": "cpu"},
        )
        second = WebhookEvent(
            source="prometheus",
            timestamp=now + timedelta(seconds=1),
            parsed_data={"RuleName": "cpu"},
        )
        session.add_all([full, first, second])

    async with session_factory() as session:
        with (
            patch("services.incidents.grouping.session_scope", return_value=_Scope(session)),
            patch(
                "services.incidents.summary.run_pending_incident_summaries",
                new=AsyncMock(return_value={"claimed": 0, "completed": 0, "failed": 0}),
            ),
        ):
            stats = await run_incident_grouping()

    async with session_factory() as session:
        memberships = list((await session.execute(select(IncidentMember))).scalars().all())
    assert stats["created"] == 1
    assert len(memberships) == 2
    incident_ids = {membership.incident_id for membership in memberships}
    assert len(incident_ids) == 1
    assert full.id not in incident_ids


@pytest.mark.asyncio
async def test_new_incident_notification_is_committed_to_outbox_before_scheduling(
    session_factory: async_sessionmaker[AsyncSession],
    temp_config: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from models import ForwardOutbox, Incident, WebhookEvent
    from services.incidents.grouping import run_incident_grouping
    from services.incidents.notifications import queue_incident_notifications

    monkeypatch.setattr(
        temp_config.notifications,
        "DEEP_ANALYSIS_FEISHU_WEBHOOK",
        "https://open.feishu.cn/open-apis/bot/v2/hook/test-token",
    )
    now = utcnow()
    async with session_factory.begin() as session:
        session.add_all(
            [
                WebhookEvent(
                    source="prometheus",
                    timestamp=now,
                    parsed_data={"RuleName": "disk"},
                ),
                WebhookEvent(
                    source="prometheus",
                    timestamp=now + timedelta(seconds=1),
                    parsed_data={"RuleName": "disk"},
                ),
            ]
        )

    schedule = AsyncMock()
    async with session_factory() as session:
        with (
            patch("services.incidents.grouping.session_scope", return_value=_Scope(session)),
            patch(
                "services.forwarding.outbox_scheduling.schedule_forward_outbox_many",
                new=schedule,
            ),
            patch(
                "services.incidents.summary.run_pending_incident_summaries",
                new=AsyncMock(return_value={"claimed": 0, "completed": 0, "failed": 0}),
            ),
        ):
            await run_incident_grouping()

    async with session_factory() as session:
        records = list((await session.execute(select(ForwardOutbox))).scalars().all())
    async with session_factory.begin() as session:
        incident = (await session.execute(select(Incident))).scalar_one()
        repeated_ids = await queue_incident_notifications(session, [incident])
    assert len(records) == 1
    assert records[0].idempotency_key.startswith("incident-created:")
    assert records[0].formatted_payload["msg_type"] == "interactive"
    assert repeated_ids == [records[0].id]
    schedule.assert_awaited_once_with([records[0].id])


@pytest.mark.asyncio
async def test_summary_persists_after_external_call_returns(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from models import Incident, IncidentMember, WebhookEvent
    from schemas.analysis import IncidentSummaryResult
    from services.incidents.summary import summarize_incident

    now = utcnow()
    async with session_factory.begin() as session:
        incident = Incident(
            title="prometheus incident — cpu",
            status="quiet",
            source="prometheus",
            started_at=now,
            updated_at=now,
            alert_count=2,
            summary_status="processing",
        )
        first = WebhookEvent(source="prometheus", timestamp=now, parsed_data={"RuleName": "cpu"})
        second = WebhookEvent(
            source="prometheus",
            timestamp=now + timedelta(seconds=1),
            parsed_data={"RuleName": "cpu"},
        )
        session.add_all([incident, first, second])
        await session.flush()
        session.add_all(
            [
                IncidentMember(
                    incident_id=incident.id,
                    event_id=first.id,
                    event_timestamp=now,
                ),
                IncidentMember(
                    incident_id=incident.id,
                    event_id=second.id,
                    event_timestamp=now + timedelta(seconds=1),
                ),
            ]
        )
        incident_id = incident.id

    sessions: list[AsyncSession] = []

    def scope_factory() -> _Scope:
        session = session_factory()
        sessions.append(session)
        return _Scope(session)

    structured = IncidentSummaryResult(
        summary="CPU 告警持续出现。",
        root_cause="CPU 资源不足。",
        impact="单个服务延迟升高。",
        timeline_summary="首先出现 CPU 告警。",
        recommendations=["扩容并检查负载。"],
        confidence=0.8,
    )
    with (
        patch("services.incidents.summary.session_scope", side_effect=scope_factory),
        patch(
            "services.analysis.ai_llm_client.create_structured_completion",
            new=AsyncMock(return_value=(structured, 10, 20)),
        ),
        patch(
            "services.analysis.ai_prompt.load_incident_summary_prompt_template",
            new=AsyncMock(return_value="{alert_briefs}"),
        ),
        patch("services.incidents.summary.AIProviderPolicy.from_config") as policy_factory,
        patch("services.analysis.ai_usage.log_ai_usage", new=AsyncMock()),
    ):
        policy_factory.return_value.available = True
        policy_factory.return_value.model = "test-model"
        result = await summarize_incident(incident_id)

    for session in sessions:
        await session.close()
    async with session_factory() as session:
        persisted = await session.get(Incident, incident_id)
    assert result is not None
    assert persisted is not None
    assert persisted.summary_status == "completed"
    assert persisted.summary_analysis["confidence"] == 0.8


def test_cross_source_service_identity_and_recovery_detection() -> None:
    from models import WebhookEvent
    from services.incidents.grouping import _event_pair_score, _is_recovery_event

    now = utcnow()
    prometheus = WebhookEvent(
        source="prometheus",
        timestamp=now,
        parsed_data={"RuleName": "HighLatency", "labels": {"service": "checkout", "environment": "prod"}},
    )
    grafana = WebhookEvent(
        source="grafana",
        timestamp=now,
        parsed_data={"AlertName": "CheckoutErrors", "service": "checkout", "env": "prod"},
    )
    recovered = WebhookEvent(
        source="prometheus",
        timestamp=now,
        parsed_data={"RuleName": "HighLatency", "status": "resolved"},
    )
    broken = WebhookEvent(source="prometheus", timestamp=now, parsed_data={"status": "broken"})

    assert _event_pair_score(prometheus, grafana) >= 0.9
    stage = WebhookEvent(
        source="prometheus",
        timestamp=now,
        parsed_data={"RuleName": "HighLatency", "labels": {"service": "checkout", "environment": "stage"}},
    )
    assert _event_pair_score(prometheus, stage) == 0.0
    assert _is_recovery_event(recovered) is True
    assert _is_recovery_event(broken) is False

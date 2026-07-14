"""Incident read API, operator actions, and summary retry contracts."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.datetime_utils import utcnow


@pytest.fixture
def session_factory(db_session_factory):
    return db_session_factory


class _AutoScope:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.session = factory()

    async def __aenter__(self) -> AsyncSession:
        return self.session

    async def __aexit__(self, *args: object) -> None:
        if args and args[0] is None:
            await self.session.commit()
        else:
            await self.session.rollback()
        await self.session.close()


async def _seed_incident(session_factory: async_sessionmaker[AsyncSession]) -> int:
    from models import Incident, IncidentMember, WebhookEvent

    now = utcnow()
    async with session_factory.begin() as session:
        incident = Incident(
            title="prometheus incident — cpu",
            status="quiet",
            source="prometheus",
            started_at=now,
            updated_at=now,
            alert_count=2,
            top_importance="high",
            summary_analysis={"summary": "CPU overloaded"},
            summary_status="completed",
        )
        older = WebhookEvent(
            source="prometheus",
            timestamp=now,
            importance="medium",
            ai_analysis={"summary": "CPU warning"},
        )
        newer = WebhookEvent(
            source="prometheus",
            timestamp=now,
            importance="high",
            ai_analysis={"summary": "CPU critical"},
            is_duplicate=True,
        )
        session.add_all([incident, older, newer])
        await session.flush()
        session.add_all(
            [
                IncidentMember(incident_id=incident.id, event_id=older.id, event_timestamp=older.timestamp),
                IncidentMember(incident_id=incident.id, event_id=newer.id, event_timestamp=newer.timestamp),
            ]
        )
        return int(incident.id)


@pytest.mark.asyncio
async def test_incident_queries_return_paginated_timeline_and_summary(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from models import Incident
    from services.incidents.queries import get_incident_detail, get_incident_summary, list_incidents

    incident_id = await _seed_incident(session_factory)
    async with session_factory.begin() as session:
        session.add(
            Incident(
                title="second",
                status="active",
                source="grafana",
                started_at=utcnow(),
                updated_at=utcnow(),
                alert_count=2,
            )
        )

    async with session_factory() as session:
        rows, has_more, next_cursor = await list_incidents(session, page_size=1)
        detail = await get_incident_detail(session, incident_id)
        summary = await get_incident_summary(session, incident_id)
        missing = await get_incident_detail(session, 99999)

    assert len(rows) == 1
    assert has_more is True
    assert next_cursor == rows[0]["id"]
    assert detail is not None
    assert len(detail["member_ids"]) == 2
    assert detail["members"][1]["is_duplicate"] is True
    assert summary is not None
    assert summary["summary_analysis"]["summary"] == "CPU overloaded"
    assert missing is None


@pytest.mark.asyncio
async def test_incident_operator_endpoints_commit_state_and_activity(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from api.v1.incidents import close_incident_endpoint, reopen_incident_endpoint
    from models import AuditLog, Incident

    incident_id = await _seed_incident(session_factory)
    async with session_factory() as session:
        incident = await session.get(Incident, incident_id)
        assert incident is not None
        incident.summary_analysis = None
        incident.summary_status = "failed"
        incident.summary_attempts = 5
        incident.summary_last_error = "old failure"
        await session.commit()
        closed = await close_incident_endpoint(incident_id, session)
        assert incident.summary_status == "pending"
        assert incident.summary_attempts == 0
        assert incident.summary_last_error is None
        reopened = await reopen_incident_endpoint(incident_id, session)
        assert incident.summary_status is None
        assert incident.summary_next_attempt_at is None
        missing = await close_incident_endpoint(99999, session)

    async with session_factory() as session:
        incident = await session.get(Incident, incident_id)
        activity = list((await session.execute(select(AuditLog).order_by(AuditLog.id))).scalars())

    assert closed.status_code == 200
    assert reopened.status_code == 200
    assert missing.status_code == 404
    assert incident is not None
    assert incident.status == "active"
    assert incident.ended_at is None
    assert [row.action for row in activity] == ["closed", "reopened"]


@pytest.mark.asyncio
async def test_incident_read_and_manual_summary_endpoints(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from api.v1.incidents import (
        get_incident_detail_endpoint,
        get_incident_summary_endpoint,
        list_incidents_endpoint,
        trigger_incident_summary_endpoint,
    )

    incident_id = await _seed_incident(session_factory)
    async with session_factory() as session:
        listed = await list_incidents_endpoint(
            cursor=None,
            status="",
            page=1,
            page_size=30,
            session=session,
        )
        detailed = await get_incident_detail_endpoint(incident_id, session)
        summarized = await get_incident_summary_endpoint(incident_id, session)
        missing = await get_incident_summary_endpoint(99999, session)

    with patch(
        "services.incidents.summary.summarize_incident",
        new=AsyncMock(return_value={"id": incident_id, "summary_analysis": {"summary": "done"}}),
    ):
        triggered = await trigger_incident_summary_endpoint(incident_id)
    with patch(
        "services.incidents.summary.summarize_incident",
        new=AsyncMock(return_value=None),
    ):
        unavailable = await trigger_incident_summary_endpoint(incident_id)

    assert json.loads(listed.body)["success"] is True
    assert json.loads(detailed.body)["data"]["id"] == incident_id
    assert json.loads(summarized.body)["data"]["summary_analysis"]["summary"] == "CPU overloaded"
    assert missing.status_code == 404
    assert triggered.status_code == 200
    assert unavailable.status_code == 409


@pytest.mark.asyncio
async def test_summary_claim_retry_and_failure_state(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from models import Incident
    from services.incidents.summary import _claim_pending_summaries, _mark_summary_retry

    now = utcnow()
    async with session_factory.begin() as session:
        incident = Incident(
            title="pending",
            status="quiet",
            source="prometheus",
            started_at=now,
            updated_at=now,
            alert_count=2,
            summary_status="pending",
            summary_next_attempt_at=now,
        )
        session.add(incident)
        await session.flush()
        incident_id = int(incident.id)

    with patch(
        "services.incidents.summary.session_scope",
        side_effect=lambda: _AutoScope(session_factory),
    ):
        claimed = await _claim_pending_summaries()
        await _mark_summary_retry(incident_id, RuntimeError("provider unavailable"))

    async with session_factory() as session:
        incident = await session.get(Incident, incident_id)
        assert incident is not None
        assert claimed == [incident_id]
        assert incident.summary_attempts == 1
        assert incident.summary_status == "retrying"
        assert incident.summary_last_error == "provider unavailable"
        incident.summary_attempts = 5
        await session.commit()

    with patch(
        "services.incidents.summary.session_scope",
        side_effect=lambda: _AutoScope(session_factory),
    ):
        await _mark_summary_retry(incident_id, "final failure")

    async with session_factory() as session:
        incident = await session.get(Incident, incident_id)
    assert incident is not None
    assert incident.summary_status == "failed"
    assert incident.summary_next_attempt_at is None


@pytest.mark.asyncio
async def test_pending_summary_batch_records_success_and_failure() -> None:
    from services.incidents.summary import run_pending_incident_summaries

    retry = AsyncMock()
    with (
        patch(
            "services.incidents.summary.AIProviderPolicy.from_config",
            return_value=SimpleNamespace(available=True),
        ),
        patch(
            "services.incidents.summary._claim_pending_summaries",
            new=AsyncMock(return_value=[1, 2]),
        ),
        patch(
            "services.incidents.summary._skip_ineligible_summaries",
            new=AsyncMock(return_value=0),
        ),
        patch(
            "services.incidents.summary.summarize_incident",
            new=AsyncMock(side_effect=[{"id": 1}, RuntimeError("LLM failed")]),
        ),
        patch("services.incidents.summary._mark_summary_retry", new=retry),
    ):
        stats = await run_pending_incident_summaries()

    assert stats == {"claimed": 2, "completed": 1, "failed": 1}
    retry.assert_awaited_once()


@pytest.mark.asyncio
async def test_incident_summary_prompt_is_file_backed_and_reloadable() -> None:
    from services.analysis.ai_prompt import (
        get_prompt_source,
        reload_incident_summary_prompt_template,
    )

    template = await reload_incident_summary_prompt_template()

    assert "{alert_briefs}" in template
    assert get_prompt_source("incident_summary").startswith("file:")

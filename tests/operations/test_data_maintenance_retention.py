from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.datetime_utils import utcnow
from services.operations.policies import DataMaintenancePolicy


@pytest.fixture
def session_factory(
    db_app_context_session_factory: async_sessionmaker[AsyncSession],
) -> async_sessionmaker[AsyncSession]:
    return db_app_context_session_factory


@pytest.mark.asyncio
async def test_secondary_retention_bounds_history_and_closes_quiet_incidents(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from models import AIUsageLog, ArchivedWebhookEvent, ForwardOutbox, Incident
    from services.operations.data_maintenance import cleanup_expired_operational_data

    now = utcnow()
    old = now - timedelta(days=120)
    recent = now - timedelta(days=1)
    async with session_factory.begin() as session:
        session.add_all(
            [
                ArchivedWebhookEvent(id=1, source="test", timestamp=old, archived_at=old),
                ArchivedWebhookEvent(id=2, source="test", timestamp=recent, archived_at=recent),
                AIUsageLog(timestamp=old, model="old"),
                AIUsageLog(timestamp=recent, model="recent"),
                ForwardOutbox(
                    idempotency_key="old-terminal",
                    target_type="webhook",
                    status="sent",
                    created_at=old,
                    updated_at=old,
                ),
                ForwardOutbox(
                    idempotency_key="recent-terminal",
                    target_type="webhook",
                    status="sent",
                    created_at=recent,
                    updated_at=recent,
                ),
                Incident(
                    title="old quiet incident",
                    status="quiet",
                    source="test",
                    started_at=old,
                    updated_at=old,
                ),
            ]
        )

    result = await cleanup_expired_operational_data(
        policy=DataMaintenancePolicy(
            enabled=True,
            retention_days_default=30,
            retention_policies={},
            source_retention_policies={},
            cleanup_keywords={},
            archive_retention_days=90,
            terminal_outbox_retention_days=30,
            ai_usage_retention_days=90,
            incident_auto_close_days=7,
        )
    )

    assert result == {"archives": 1, "outboxes": 1, "ai_usage": 1, "incidents_closed": 1}
    async with session_factory() as session:
        assert await session.scalar(select(func.count(ArchivedWebhookEvent.id))) == 1
        assert await session.scalar(select(func.count(AIUsageLog.id))) == 1
        assert await session.scalar(select(func.count(ForwardOutbox.id))) == 1
        incident = (await session.execute(select(Incident))).scalar_one()
    assert incident.status == "closed"
    assert incident.workflow_status == "resolved"


@pytest.mark.asyncio
async def test_event_archival_preserves_live_child_histories(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from models import (
        ArchivedWebhookEvent,
        DeepAnalysis,
        ForwardOutbox,
        Incident,
        IncidentMember,
        WebhookEvent,
    )
    from services.operations.data_maintenance import cleanup_old_data_by_policy

    old = utcnow() - timedelta(days=60)
    async with session_factory.begin() as session:
        events = [
            WebhookEvent(request_id=f"retention-{name}", source="test", timestamp=old)
            for name in ("free", "outbox", "analysis", "incident")
        ]
        session.add_all(events)
        await session.flush()
        free, with_outbox, with_analysis, with_incident = events
        incident = Incident(title="retained incident", status="closed", started_at=old, ended_at=old)
        session.add(incident)
        await session.flush()
        session.add_all(
            [
                ForwardOutbox(
                    idempotency_key="retained-outbox",
                    webhook_event_id=with_outbox.id,
                    target_type="webhook",
                    status="sent",
                    created_at=old,
                    updated_at=utcnow(),
                ),
                DeepAnalysis(webhook_event_id=with_analysis.id, status="completed"),
                IncidentMember(
                    incident_id=incident.id,
                    event_id=with_incident.id,
                    event_timestamp=old,
                ),
            ]
        )
        protected_ids = {with_outbox.id, with_analysis.id, with_incident.id}

    archived = await cleanup_old_data_by_policy(
        policy=DataMaintenancePolicy(
            enabled=True,
            retention_days_default=30,
            retention_policies={},
            source_retention_policies={},
            cleanup_keywords={},
        )
    )

    async with session_factory() as session:
        remaining_ids = set((await session.scalars(select(WebhookEvent.id))).all())
        archived_ids = set((await session.scalars(select(ArchivedWebhookEvent.id))).all())
        assert await session.scalar(select(func.count(ForwardOutbox.id))) == 1
        assert await session.scalar(select(func.count(DeepAnalysis.id))) == 1
        assert await session.scalar(select(func.count(IncidentMember.id))) == 1

    assert archived == 1
    assert archived_ids == {free.id}
    assert remaining_ids == protected_ids


@pytest.mark.asyncio
async def test_event_archival_propagates_transaction_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.operations import data_maintenance

    @asynccontextmanager
    async def failed_scope() -> AsyncIterator[AsyncSession]:
        raise RuntimeError("commit failed")
        yield  # pragma: no cover

    monkeypatch.setattr(data_maintenance, "session_scope", failed_scope)

    with pytest.raises(RuntimeError, match="commit failed"):
        await data_maintenance.cleanup_old_data_by_policy(
            policy=DataMaintenancePolicy(
                enabled=True,
                retention_days_default=30,
                retention_policies={},
                source_retention_policies={},
                cleanup_keywords={},
            )
        )

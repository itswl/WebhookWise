from __future__ import annotations

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

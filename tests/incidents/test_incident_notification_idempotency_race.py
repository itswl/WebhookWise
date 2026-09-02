"""Incident grouping may run on two workers; both can pass the incident-created pre-check.

The loser's INSERT then hits the UNIQUE idempotency key. It must adopt the
row the winner committed instead of failing the whole incident transaction.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.datetime_utils import utcnow
from models import ForwardOutbox, Incident
from services.incidents import notifications


@pytest.mark.asyncio
async def test_incident_created_intent_adopts_the_row_a_concurrent_worker_committed(
    db_session_factory: async_sessionmaker[AsyncSession],
    temp_config: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        temp_config.notifications,
        "DEEP_ANALYSIS_FEISHU_WEBHOOK",
        "https://open.feishu.cn/open-apis/bot/v2/hook/test-token",
    )
    now = utcnow()
    async with db_session_factory.begin() as session:
        incident = Incident(
            title="prometheus incident — disk", status="active", source="prometheus", started_at=now, updated_at=now
        )
        session.add(incident)
        await session.flush()
        incident_id = int(incident.id)
        # What the other worker committed a moment ago.
        session.add(
            ForwardOutbox(
                idempotency_key=f"incident-created:{incident_id}",
                target_type="feishu",
                target_url="https://open.feishu.cn/open-apis/bot/v2/hook/test-token",
                channel_name="feishu",
                event_type="incident_created",
                status="pending",
                attempts=0,
                max_attempts=3,
            )
        )
    async with db_session_factory() as session:
        winner_id = (await session.execute(select(ForwardOutbox.id))).scalar_one()

    async def pre_check_that_ran_before_the_competing_commit(session: AsyncSession, key: str) -> int | None:
        return None

    monkeypatch.setattr(notifications, "find_outbox_id_by_key", pre_check_that_ran_before_the_competing_commit)

    async with db_session_factory.begin() as session:
        incident = await session.get(Incident, incident_id)
        assert incident is not None
        ids = await notifications.queue_incident_notifications(session, [incident])
        # The incident transaction survives the rolled-back SAVEPOINT.
        incident.alert_count = 3

    assert ids == [winner_id]
    async with db_session_factory() as session:
        assert list((await session.execute(select(ForwardOutbox.id))).scalars().all()) == [winner_id]
        refreshed = await session.get(Incident, incident_id)
        assert refreshed is not None and refreshed.alert_count == 3

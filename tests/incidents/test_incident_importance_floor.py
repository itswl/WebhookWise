"""An incident has to earn its name: the grouping floor and the probe exclusion."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.datetime_utils import utcnow
from models import Incident, WebhookEvent
from services.incidents import grouping
from services.operations import runtime_settings as rt


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


async def _run_grouping(session_factory: async_sessionmaker[AsyncSession]) -> dict[str, int]:
    async with session_factory() as session:
        with (
            patch("services.incidents.grouping.session_scope", return_value=_Scope(session)),
            patch(
                "services.incidents.summary.run_pending_incident_summaries",
                new=AsyncMock(return_value={"claimed": 0, "completed": 0, "failed": 0}),
            ),
        ):
            return await grouping.run_incident_grouping()


def _alert(*, source: str, minutes_ago: int, importance: str | None, status: str = "firing") -> WebhookEvent:
    return WebhookEvent(
        source=source,
        timestamp=utcnow() - timedelta(minutes=minutes_ago),
        parsed_data={"RuleName": "Example deposit threshold", "service": "payments", "status": status},
        importance=importance,
        processing_status="completed",
    )


@pytest.mark.parametrize(
    ("floor", "importance", "expected"),
    [
        ("low", "low", True),
        ("medium", "low", False),
        ("medium", "medium", True),
        ("high", "medium", False),
        ("high", "high", True),
        ("high", None, True),  # unknown importance fails open
        ("high", "", True),
        ("high", "weird", True),
    ],
)
def test_importance_floor_decides_whether_an_alert_can_group(
    floor: str, importance: str | None, expected: bool
) -> None:
    assert grouping._importance_opens_incident(importance, floor) is expected


def test_floor_reads_runtime_override_then_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rt, "override_or", lambda key, fallback: "medium")
    assert grouping._incident_min_importance() == "medium"
    monkeypatch.setattr(rt, "override_or", lambda key, fallback: "nonsense")
    assert grouping._incident_min_importance() == "low"


@pytest.mark.asyncio
async def test_low_alerts_below_the_floor_never_open_an_incident(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_factory.begin() as session:
        session.add_all([_alert(source="grafana", minutes_ago=4, importance="low") for _ in range(2)])

    monkeypatch.setattr(grouping, "_incident_min_importance", lambda: "medium")
    stats = await _run_grouping(session_factory)

    assert stats["created"] == 0
    async with session_factory() as session:
        assert (await session.execute(select(func.count(Incident.id)))).scalar_one() == 0


@pytest.mark.asyncio
async def test_the_same_pair_still_groups_at_the_default_floor(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_factory.begin() as session:
        session.add_all([_alert(source="grafana", minutes_ago=4, importance="low") for _ in range(2)])

    monkeypatch.setattr(grouping, "_incident_min_importance", lambda: "low")
    stats = await _run_grouping(session_factory)

    assert stats["created"] == 1


@pytest.mark.asyncio
async def test_a_recovery_resolves_its_incident_whatever_its_importance(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = utcnow()
    async with session_factory.begin() as session:
        incident = Incident(
            title="grafana incident — Example deposit threshold",
            status="active",
            source="grafana",
            started_at=now - timedelta(minutes=6),
            updated_at=now - timedelta(minutes=6),
            alert_count=2,
            top_importance="high",
            workflow_status="open",
            correlation_dimensions={"service": "payments"},
            correlation_confidence=1.0,
        )
        session.add(incident)
        await session.flush()
        incident_id = int(incident.id)
        session.add(_alert(source="grafana", minutes_ago=1, importance="low", status="resolved"))

    # The strictest possible floor: a recovery must still be able to close what
    # a firing opened, or an incident could be created but never resolved.
    monkeypatch.setattr(grouping, "_incident_min_importance", lambda: "high")
    await _run_grouping(session_factory)

    async with session_factory() as session:
        persisted = await session.get(Incident, incident_id)
    assert persisted is not None
    assert persisted.status == "closed"
    assert persisted.workflow_status == "resolved"


@pytest.mark.asyncio
async def test_a_synthetic_source_never_opens_an_incident(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory.begin() as session:
        session.add_all([_alert(source="rotation-probe", minutes_ago=3, importance="high") for _ in range(3)])

    rt._swap_snapshot({"SYNTHETIC_SOURCES": "Rotation-Probe"})  # matched case-insensitively
    try:
        stats = await _run_grouping(session_factory)
    finally:
        rt._reset_snapshot_for_tests()

    assert stats["created"] == 0
    async with session_factory() as session:
        assert (await session.execute(select(func.count(Incident.id)))).scalar_one() == 0
        # Still stored: a probe that is dropped tests nothing downstream of it.
        assert (await session.execute(select(func.count(WebhookEvent.id)))).scalar_one() == 3

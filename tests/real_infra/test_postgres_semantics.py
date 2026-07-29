"""Semantics only real PostgreSQL can verify (SQLite is silently lenient)."""

from __future__ import annotations

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.datetime_utils import utcnow
from models import Incident, Silence
from scripts.healthcheck import _expected_migration_heads

pytestmark = pytest.mark.real_services


@pytest.mark.asyncio
async def test_migration_chain_applies_and_head_matches(pg_session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with pg_session_factory() as session:
        version = (await session.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
    assert {version} == _expected_migration_heads()


@pytest.mark.asyncio
async def test_mw_partial_unique_index_enforced(pg_session_factory: async_sessionmaker[AsyncSession]) -> None:
    """The occurrence identity race-guard must exist on real Postgres."""
    async with pg_session_factory() as session:
        session.add(
            Silence(
                match_source="pg-mw",
                created_by="maintenance-window",
                mw_window_id=901,
                mw_occurrence_date="2026-07-26",
                mw_schedule_digest="digest-a",
            )
        )
        await session.commit()

    async with pg_session_factory() as session:
        session.add(
            Silence(
                match_source="pg-mw",
                created_by="maintenance-window",
                mw_window_id=901,
                mw_occurrence_date="2026-07-26",
                mw_schedule_digest="digest-a",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()

    # NULL identity rows (ordinary silences) are outside the partial index.
    async with pg_session_factory() as session:
        session.add(Silence(match_source="plain-1"))
        session.add(Silence(match_source="plain-2"))
        await session.commit()


@pytest.mark.asyncio
async def test_for_update_actually_locks(pg_session_factory: async_sessionmaker[AsyncSession]) -> None:
    """SQLite compiles FOR UPDATE away — the card-action row lock is only
    provable on Postgres: a second NOWAIT locker must fail immediately."""
    async with pg_session_factory() as session:
        incident = Incident(
            title="lock probe",
            status="active",
            started_at=utcnow(),
            alert_count=1,
            workflow_status="open",
            correlation_dimensions={},
            correlation_confidence=1.0,
        )
        session.add(incident)
        await session.commit()
        incident_id = int(incident.id)

    async with pg_session_factory() as holder, pg_session_factory() as contender:
        locked = (
            await holder.execute(select(Incident).where(Incident.id == incident_id).with_for_update())
        ).scalar_one()
        assert locked.id == incident_id
        with pytest.raises(DBAPIError):
            await contender.execute(select(Incident).where(Incident.id == incident_id).with_for_update(nowait=True))

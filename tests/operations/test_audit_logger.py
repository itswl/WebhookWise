"""Transactional activity-log guarantees."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.fixture
def session_factory(db_session_factory):
    return db_session_factory


@pytest.mark.asyncio
async def test_audit_row_follows_business_transaction(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from models import AuditLog
    from services.operations.audit_logger import add_audit

    async with session_factory() as session:
        add_audit(session, "incident", 1, "test", "closed", "rolled back")
        await session.flush()
        await session.rollback()

    async with session_factory() as session:
        assert list((await session.execute(select(AuditLog))).scalars()) == []
        add_audit(session, "incident", 1, "test", "closed", "committed")
        await session.commit()

    async with session_factory() as session:
        rows = list((await session.execute(select(AuditLog))).scalars())
    assert [row.summary for row in rows] == ["committed"]

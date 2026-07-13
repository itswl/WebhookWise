"""Transactional activity-log guarantees."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    from db.session import Base

    engine = create_async_engine("sqlite+aiosqlite://", connect_args={"check_same_thread": False})
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


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

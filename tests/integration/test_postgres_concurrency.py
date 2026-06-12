"""Real-PostgreSQL concurrency tests for the dedup advisory lock.

These validate the invariant that the SQLite-based unit tests structurally
cannot: that acquire_advisory_xact_lock actually serialises concurrent
transactions on the same key (it no-ops off PostgreSQL). Skipped unless
POSTGRES_TEST_URL points at a reachable PostgreSQL.

Run locally, e.g.:
    POSTGRES_TEST_URL=postgresql+asyncpg://user:pass@localhost:5432/test \
        pytest tests/integration/test_postgres_concurrency.py
"""

from __future__ import annotations

import asyncio
import os

import pytest

_PG_URL = os.environ.get("POSTGRES_TEST_URL", "")
pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not _PG_URL, reason="POSTGRES_TEST_URL not set"),
]


@pytest.mark.asyncio
async def test_advisory_xact_lock_serialises_same_key() -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from db.session import acquire_advisory_xact_lock

    engine = create_async_engine(_PG_URL)
    sm = async_sessionmaker(bind=engine, expire_on_commit=False)

    order: list[str] = []
    # A barrier so both coroutines reach the lock acquisition before either holds it.
    started = asyncio.Event()

    async def worker(tag: str, hold: float) -> None:
        async with sm() as session, session.begin():
            await acquire_advisory_xact_lock(session, "concurrency-test-key")
            order.append(f"{tag}:acquired")
            started.set()
            await asyncio.sleep(hold)  # hold the lock inside the txn
            order.append(f"{tag}:released")

    try:
        a = asyncio.create_task(worker("A", 0.4))
        await started.wait()  # ensure A holds the lock first
        b = asyncio.create_task(worker("B", 0.0))
        await asyncio.gather(a, b)
    finally:
        await engine.dispose()

    # B must not acquire until A releases (transaction-scoped lock).
    assert order == ["A:acquired", "A:released", "B:acquired", "B:released"], order


@pytest.mark.asyncio
async def test_advisory_xact_lock_different_keys_do_not_block() -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from db.session import acquire_advisory_xact_lock

    engine = create_async_engine(_PG_URL)
    sm = async_sessionmaker(bind=engine, expire_on_commit=False)

    order: list[str] = []

    async def worker(tag: str, key: str, hold: float) -> None:
        async with sm() as session, session.begin():
            await acquire_advisory_xact_lock(session, key)
            order.append(f"{tag}:acquired")
            await asyncio.sleep(hold)

    try:
        # Different keys -> no contention; B can acquire while A still holds.
        a = asyncio.create_task(worker("A", "key-1", 0.3))
        await asyncio.sleep(0.05)
        b = asyncio.create_task(worker("B", "key-2", 0.0))
        await asyncio.gather(a, b)
    finally:
        await engine.dispose()

    assert "B:acquired" in order

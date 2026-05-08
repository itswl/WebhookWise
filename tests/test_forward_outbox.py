from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_: object, compiler: object, **kw: object) -> str:
    return "JSON"


@pytest.fixture()
async def session_factory(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    import db.session as db_session
    import models  # noqa: F401
    from db.session import Base

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(db_session, "_engine", engine)
    monkeypatch.setattr(db_session, "_session_factory", factory)

    yield factory
    await engine.dispose()


async def test_create_forward_outbox_records_is_idempotent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from models import ForwardOutbox
    from services.forwarding.outbox import create_forward_outbox_records
    from services.webhooks.types import ForwardDecision

    decision = ForwardDecision(
        should_forward=True,
        skip_reason=None,
        is_periodic_reminder=False,
        matched_rules=[{"id": 7, "name": "ops", "target_type": "webhook", "target_url": "https://example.test/hook"}],
    )

    async with session_factory.begin() as session:
        first_ids = await create_forward_outbox_records(
            session,
            decision=decision,
            full_data={"source": "test"},
            analysis={"summary": "x"},
            webhook_id=1,
            orig_id=None,
        )
        second_ids = await create_forward_outbox_records(
            session,
            decision=decision,
            full_data={"source": "test"},
            analysis={"summary": "x"},
            webhook_id=1,
            orig_id=None,
        )

    async with session_factory() as session:
        rows = (await session.execute(select(ForwardOutbox))).scalars().all()

    assert len(first_ids) == 1
    assert second_ids == []
    assert len(rows) == 1
    assert rows[0].status == "pending"


async def test_process_forward_outbox_marks_sent(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from models import ForwardOutbox
    from services.forwarding.outbox import process_forward_outbox_by_id

    async with session_factory.begin() as session:
        record = ForwardOutbox(
            idempotency_key="forward:test",
            webhook_event_id=1,
            target_type="webhook",
            target_url="https://example.test/hook",
            status="pending",
            attempts=0,
            max_attempts=2,
            forward_data={"source": "test"},
            analysis_result={"summary": "x"},
        )
        session.add(record)
        await session.flush()
        outbox_id = record.id

    async def fake_forward_to_remote(**_: Any) -> dict[str, Any]:
        return {"status": "success", "status_code": 200}

    monkeypatch.setattr("services.forwarding.forward.forward_to_remote", fake_forward_to_remote)

    await process_forward_outbox_by_id(outbox_id)

    async with session_factory() as session:
        updated = await session.get(ForwardOutbox, outbox_id)

    assert updated is not None
    assert updated.status == "sent"
    assert updated.attempts == 1

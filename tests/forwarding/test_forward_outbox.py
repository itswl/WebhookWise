from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.fixture
def session_factory(
    db_app_context_session_factory: async_sessionmaker[AsyncSession],
) -> async_sessionmaker[AsyncSession]:
    return db_app_context_session_factory


async def test_resolve_and_forward_is_idempotent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from models import ForwardOutbox
    from services.forwarding.outbox import resolve_and_forward
    from services.webhooks.decisioning import ForwardDecision, ForwardRuleSnapshot

    decision = ForwardDecision(
        should_forward=True,
        skip_reason=None,
        is_periodic_reminder=False,
        matched_rules=[
            ForwardRuleSnapshot(
                id=7,
                name="ops",
                match_event_type="",
                match_importance="",
                match_source="",
                match_duplicate="all",
                match_payload="",
                target_type="webhook",
                target_url="https://example.test/hook",
                stop_on_match=False,
            )
        ],
    )

    async with session_factory.begin() as session:
        first = await resolve_and_forward(
            session=session,
            decision=decision,
            forward_data={"source": "test"},
            analysis_result={"summary": "x"},
            webhook_id=1,
            orig_id=None,
        )
        second = await resolve_and_forward(
            session=session,
            decision=decision,
            forward_data={"source": "test"},
            analysis_result={"summary": "x"},
            webhook_id=1,
            orig_id=None,
        )

    async with session_factory() as session:
        rows = (await session.execute(select(ForwardOutbox))).scalars().all()

    first_ids = list(first.get("outbox_ids") or [])
    second_ids = list(second.get("outbox_ids") or [])
    assert len(first_ids) == 1
    assert second_ids == first_ids  # idempotent: returns the same ids
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
            channel_name="webhook",
            event_type="webhook_forward",
            status="pending",
            attempts=0,
            max_attempts=2,
            formatted_payload={"hello": "world"},
        )
        session.add(record)
        await session.flush()
        outbox_id = record.id

    async def fake_post_json_to_remote(*_: Any, **__: Any) -> dict[str, Any]:
        return {"status": "success", "status_code": 200}

    monkeypatch.setattr("services.forwarding.remote.post_json_to_remote", fake_post_json_to_remote)

    await process_forward_outbox_by_id(outbox_id)

    async with session_factory() as session:
        updated = await session.get(ForwardOutbox, outbox_id)

    assert updated is not None
    assert updated.status == "sent"
    assert updated.attempts == 1

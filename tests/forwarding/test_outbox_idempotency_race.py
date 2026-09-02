"""Two workers can both pass the idempotency pre-check before either commits.

The UNIQUE index on forward_outboxes.idempotency_key then rejects the loser at
flush time. That IntegrityError used to escape create_outbox_records, poison
the caller's transaction and record the webhook's persist stage as failed; the
loser must instead adopt the winner's row, exactly like a pre-check hit.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from models import ForwardOutbox
from services.forwarding import outbox_records
from services.forwarding.policies import ForwardDeliveryPolicy
from services.forwarding.types import ForwardRuleSnapshot

_TARGET = "https://example.test/hook"
_RULE = ForwardRuleSnapshot(
    id=7,
    name="ops",
    match_event_type="",
    match_importance="",
    match_source="",
    match_duplicate="all",
    match_payload="",
    target_type="webhook",
    target_url=_TARGET,
    stop_on_match=False,
    target_name="ops",
)
_KEY = outbox_records.idempotency_key(
    webhook_id=1, rule_id=7, target_type="webhook", target_url=_TARGET, is_periodic_reminder=False
)


def _row(key: str = _KEY, **overrides: Any) -> ForwardOutbox:
    """The row a competing worker commits (or the one this worker tries to)."""
    values: dict[str, Any] = {
        "idempotency_key": key,
        "target_type": "webhook",
        "target_url": _TARGET,
        "channel_name": "webhook",
        "event_type": "webhook_forward",
        "status": "pending",
        "attempts": 0,
        "max_attempts": 3,
    }
    values.update(overrides)
    return ForwardOutbox(**values)


async def _commit_winner(factory: async_sessionmaker[AsyncSession]) -> int:
    async with factory.begin() as session:
        winner = _row()
        session.add(winner)
        await session.flush()
        return int(winner.id)


def _pre_check_misses_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first lookup ran before the competing commit landed; later ones see its row."""
    real = outbox_records.find_outbox_id_by_key
    calls = {"n": 0}

    async def racing(session: AsyncSession, key: str) -> int | None:
        calls["n"] += 1
        return None if calls["n"] == 1 else await real(session, key)

    monkeypatch.setattr(outbox_records, "find_outbox_id_by_key", racing)


async def _keys(factory: async_sessionmaker[AsyncSession]) -> list[str]:
    async with factory() as session:
        result = await session.execute(select(ForwardOutbox.idempotency_key).order_by(ForwardOutbox.id))
        return list(result.scalars().all())


@pytest.mark.asyncio
async def test_losing_the_insert_race_adopts_the_winners_row(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    winner_id = await _commit_winner(db_session_factory)
    _pre_check_misses_once(monkeypatch)

    async with db_session_factory.begin() as session:
        ids = await outbox_records.create_outbox_records(
            session,
            [_RULE],
            webhook_id=1,
            orig_id=None,
            forward_data={"source": "test"},
            analysis_result={"importance": "high", "summary": "s"},
            formatted_payload=None,
            event_type="webhook_forward",
            is_periodic_reminder=False,
            policy=ForwardDeliveryPolicy.from_config(),
            log_tag="test",
        )
        # The transaction survived the rolled-back SAVEPOINT and keeps working.
        session.add(_row(key="unrelated"))
        await session.flush()

    assert ids == [winner_id]
    assert await _keys(db_session_factory) == [_KEY, "unrelated"]


@pytest.mark.asyncio
async def test_pipeline_path_reports_queued_not_failed_when_the_row_already_exists(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    from services.forwarding.outbox import resolve_and_forward
    from services.webhooks.decisioning import ForwardDecision

    winner_id = await _commit_winner(db_session_factory)
    _pre_check_misses_once(monkeypatch)

    async with db_session_factory.begin() as session:
        result = await resolve_and_forward(
            session=session,
            decision=ForwardDecision(
                should_forward=True, skip_reason=None, is_periodic_reminder=False, matched_rules=[_RULE]
            ),
            forward_data={"source": "test"},
            analysis_result={"importance": "high", "summary": "s"},
            webhook_id=1,
            orig_id=None,
        )

    assert result == {"status": "queued", "outbox_ids": [winner_id], "outbox_id": winner_id}
    assert await _keys(db_session_factory) == [_KEY]


@pytest.mark.asyncio
async def test_insert_or_existing_returns_the_committed_row_on_a_key_collision(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    winner_id = await _commit_winner(db_session_factory)

    async with db_session_factory.begin() as session:
        assert await outbox_records.insert_outbox_or_existing(session, _row()) == (winner_id, False)
        fresh_id, created = await outbox_records.insert_outbox_or_existing(session, _row(key="fresh"))
        assert created is True and fresh_id != winner_id

    assert await _keys(db_session_factory) == [_KEY, "fresh"]


@pytest.mark.asyncio
async def test_other_integrity_errors_still_propagate(db_session_factory: async_sessionmaker[AsyncSession]) -> None:
    """A NOT NULL violation has no winner's row to adopt; swallowing it would hide a real bug."""
    async with db_session_factory.begin() as session:
        with pytest.raises(IntegrityError):
            await outbox_records.insert_outbox_or_existing(session, _row(key="broken", target_type=None))

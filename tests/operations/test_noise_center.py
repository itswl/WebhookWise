"""Noise-center metrics, recommendation, apply, and undo tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from core.datetime_utils import utcnow
from db.session import Base
from models import DecisionTrace, ForwardRule, NoiseReductionAction, Silence, WebhookEvent


@pytest.fixture()
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db_session:
        yield db_session
    await engine.dispose()


async def _seed_noise(session: AsyncSession) -> ForwardRule:
    now = utcnow()
    rule = ForwardRule(
        name="Primary operations channel",
        enabled=True,
        target_type="feishu",
        target_url="https://example.com/hook",
        match_source="prometheus",
        match_duplicate="all",
    )
    session.add(rule)
    await session.flush()
    for index in range(12):
        event = WebhookEvent(
            source="prometheus",
            timestamp=now - timedelta(minutes=index),
            parsed_data={
                "RuleName": "HighCpuUsage",
                "status": "resolved" if index == 0 else "firing",
            },
            is_duplicate=index < 11,
        )
        session.add(event)
        await session.flush()
        skipped = index < 8
        session.add(
            DecisionTrace(
                webhook_event_id=event.id,
                created_at=event.timestamp,
                source="prometheus",
                outcome="skipped" if skipped else "forwarded",
                skip_code="cooldown" if skipped else "none",
                matched_rules=[] if skipped else [rule.name],
            )
        )
    await session.commit()
    return rule


@pytest.mark.asyncio
async def test_noise_center_quantifies_noise_and_builds_actionable_suggestions(session: AsyncSession) -> None:
    from services.operations.noise_center import get_noise_center

    await _seed_noise(session)
    result = await get_noise_center(session, window_days=7)

    assert result["summary"]["total"] == 12
    assert result["summary"]["duplicates"] == 11
    assert result["summary"]["duplicate_rate"] == 91.7
    assert result["summary"]["recoveries"] == 1
    assert result["summary"]["notifications_avoided"] == 8
    assert result["summary"]["estimated_minutes_saved"] == 24
    assert result["sources"][0]["source"] == "prometheus"
    assert {item["kind"] for item in result["suggestions"]} >= {"duplicate_filter", "temporary_silence"}


@pytest.mark.asyncio
async def test_duplicate_filter_action_is_durable_and_reversible(session: AsyncSession) -> None:
    from services.operations.noise_center import apply_noise_suggestion, get_noise_center, undo_noise_action

    rule = await _seed_noise(session)
    center = await get_noise_center(session, window_days=7)
    suggestion = next(item for item in center["suggestions"] if item["kind"] == "duplicate_filter")

    applied = await apply_noise_suggestion(
        session,
        suggestion_id=str(suggestion["id"]),
        window_days=7,
        actor="alice",
    )
    assert applied["changed"] is True
    await session.refresh(rule)
    assert rule.match_duplicate == "new"
    action = (await session.execute(select(NoiseReductionAction))).scalar_one()
    assert action.status == "applied"
    assert action.before_state == {"match_duplicate": "all"}

    undone = await undo_noise_action(session, action_id=int(action.id), actor="alice")
    assert undone["changed"] is True
    await session.refresh(rule)
    await session.refresh(action)
    assert rule.match_duplicate == "all"
    assert action.status == "undone"
    assert action.undone_at is not None


@pytest.mark.asyncio
async def test_temporary_silence_action_can_be_lifted_from_history(session: AsyncSession) -> None:
    from services.operations.noise_center import apply_noise_suggestion, get_noise_center, undo_noise_action

    await _seed_noise(session)
    center = await get_noise_center(session, window_days=7)
    suggestion = next(item for item in center["suggestions"] if item["kind"] == "temporary_silence")
    applied = await apply_noise_suggestion(
        session,
        suggestion_id=str(suggestion["id"]),
        window_days=7,
        actor="bob",
    )
    action_id = int(applied["action"]["id"])
    silence = (await session.execute(select(Silence))).scalar_one()
    assert silence.match_source == "prometheus"
    assert silence.match_payload == "RuleName=HighCpuUsage"
    assert silence.lifted_at is None

    undone = await undo_noise_action(session, action_id=action_id, actor="bob")
    assert undone["changed"] is True
    await session.refresh(silence)
    assert silence.lifted_at is not None

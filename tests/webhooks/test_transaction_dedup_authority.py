from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.datetime_utils import utcnow


@pytest.fixture
def session_factory(db_session_factory):
    return db_session_factory


@pytest.mark.asyncio
async def test_transaction_rechecks_caller_supplied_new_decision(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from models import WebhookEvent
    from services.webhooks.command_service import SaveWebhookInput, save_webhook_data_in_session

    common = {
        "data": {"RuleName": "HighCPU"},
        "source": "prometheus",
        "ai_analysis": {"importance": "high", "summary": "CPU high"},
        "alert_hash": "severity-sensitive-hash",
        "dedup_key": "stable-cpu-identity",
        "is_duplicate": False,
        "skip_duplicate_lookup": True,
    }
    async with session_factory.begin() as session:
        first = await save_webhook_data_in_session(
            session,
            input=SaveWebhookInput(request_id="request-1", **common),
        )
    async with session_factory.begin() as session:
        second = await save_webhook_data_in_session(
            session,
            input=SaveWebhookInput(request_id="request-2", **common),
        )

    async with session_factory() as session:
        second_event = await session.get(WebhookEvent, second.webhook_id)
    assert first.is_duplicate is False
    assert second.is_duplicate is True
    assert second.original_id == first.webhook_id
    assert second_event is not None
    assert second_event.duplicate_of == first.webhook_id


@pytest.mark.asyncio
async def test_alert_hash_is_authoritative_when_dedup_key_is_missing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from services.webhooks.command_service import SaveWebhookInput, save_webhook_data_in_session

    common = {
        "data": {"RuleName": "LegacyAlert"},
        "source": "legacy",
        "alert_hash": "legacy-alert-hash",
        "is_duplicate": False,
        "skip_duplicate_lookup": True,
    }
    async with session_factory.begin() as session:
        original = await save_webhook_data_in_session(
            session,
            input=SaveWebhookInput(request_id="legacy-1", **common),
        )
    async with session_factory.begin() as session:
        follower = await save_webhook_data_in_session(
            session,
            input=SaveWebhookInput(request_id="legacy-2", **common),
        )

    assert original.is_duplicate is False
    assert follower.is_duplicate is True
    assert follower.original_id == original.webhook_id


@pytest.mark.asyncio
async def test_only_one_rechain_wins_after_old_chain_expires(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from models import WebhookEvent
    from services.webhooks.command_service import SaveWebhookInput, save_webhook_data_in_session

    async with session_factory.begin() as session:
        previous = WebhookEvent(
            source="prometheus",
            timestamp=utcnow() - timedelta(hours=5),
            parsed_data={"RuleName": "HighCPU"},
            alert_hash="old-hash",
            dedup_key="stable-cpu-identity",
            is_duplicate=False,
            processing_status="completed",
        )
        session.add(previous)
        await session.flush()
        previous_id = previous.id

    common = {
        "data": {"RuleName": "HighCPU"},
        "source": "prometheus",
        "ai_analysis": {"importance": "high", "summary": "CPU high"},
        "alert_hash": "new-hash",
        "dedup_key": "stable-cpu-identity",
        "is_duplicate": False,
        "skip_duplicate_lookup": True,
        "prev_alert_id": previous_id,
    }
    async with session_factory.begin() as session:
        winner = await save_webhook_data_in_session(
            session,
            input=SaveWebhookInput(request_id="rechain-1", **common),
        )
    async with session_factory.begin() as session:
        follower = await save_webhook_data_in_session(
            session,
            input=SaveWebhookInput(request_id="rechain-2", **common),
        )

    assert winner.is_duplicate is False
    assert follower.is_duplicate is True
    assert follower.original_id == winner.webhook_id


@pytest.mark.asyncio
async def test_premature_rechain_cannot_bypass_recent_original(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from services.webhooks.command_service import SaveWebhookInput, save_webhook_data_in_session

    common = {
        "data": {"RuleName": "HighCPU"},
        "source": "prometheus",
        "ai_analysis": {"importance": "high", "summary": "CPU high"},
        "alert_hash": "same-hash",
        "dedup_key": "stable-cpu-identity",
        "is_duplicate": False,
        "skip_duplicate_lookup": True,
    }
    async with session_factory.begin() as session:
        original = await save_webhook_data_in_session(
            session,
            input=SaveWebhookInput(request_id="recent-original", **common),
        )
    async with session_factory.begin() as session:
        follower = await save_webhook_data_in_session(
            session,
            input=SaveWebhookInput(
                request_id="premature-rechain",
                prev_alert_id=original.webhook_id,
                **common,
            ),
        )

    assert follower.is_duplicate is True
    assert follower.original_id == original.webhook_id

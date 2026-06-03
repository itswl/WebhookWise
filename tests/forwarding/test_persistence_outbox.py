from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

from core.datetime_utils import utcnow
from services.forwarding.policies import ForwardDeliveryPolicy
from services.webhooks.types import DeepAnalysisStatus, ForwardOutboxStatus, WebhookProcessingStatus


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_: object, compiler: object, **kw: object) -> str:
    return "JSON"


@pytest.fixture()
async def session_factory(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    import models  # noqa: F401
    from core.app_context import AppContext, set_default_app_context
    from db.session import Base

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    context = AppContext(session_factory=factory)
    set_default_app_context(context)

    @asynccontextmanager
    async def scope() -> AsyncIterator[AsyncSession]:
        async with factory.begin() as session:
            yield session

    from services.forwarding import outbox
    from services.operations import data_maintenance
    from services.webhooks import command_service, repository

    monkeypatch.setattr(outbox, "session_scope", scope)
    monkeypatch.setattr(data_maintenance, "session_scope", scope)
    monkeypatch.setattr(command_service, "session_scope", scope)
    monkeypatch.setattr(repository, "session_scope", scope)
    try:
        yield factory
    finally:
        set_default_app_context(None)
        await engine.dispose()


def _policy(**overrides: object) -> ForwardDeliveryPolicy:
    values: dict[str, object] = {
        "timeout_seconds": 5,
        "max_attempts": 2,
        "retry_initial_delay": 3,
        "retry_max_delay": 30,
        "retry_backoff_multiplier": 2.0,
        "stale_processing_threshold_seconds": 60,
        "max_delivery_age_seconds": 60,
    }
    values.update(overrides)
    return ForwardDeliveryPolicy(**values)


def _rule(**overrides: object) -> Any:
    from services.webhooks.decisioning import ForwardRuleSnapshot

    values: dict[str, object] = {
        "id": 1,
        "name": "rule",
        "match_event_type": "",
        "match_importance": "",
        "match_source": "",
        "match_duplicate": "",
        "match_payload": "",
        "target_type": "webhook",
        "target_url": "https://target.test/hook?token=secret",
        "stop_on_match": True,
        "target_name": "target",
    }
    values.update(overrides)
    return ForwardRuleSnapshot(**values)


async def _insert_event(
    session: AsyncSession,
    *,
    alert_hash: str = "hash-1",
    request_id: str | None = None,
    timestamp: object | None = None,
    is_duplicate: bool = False,
    duplicate_of: int | None = None,
    processing_status: str = WebhookProcessingStatus.COMPLETED,
    ai_analysis: dict[str, object] | None = None,
) -> Any:
    from models import WebhookEvent

    event = WebhookEvent(
        request_id=request_id,
        source="prometheus",
        timestamp=timestamp or utcnow(),
        parsed_data={"RuleName": "HighCPU"},
        raw_payload=b'{"RuleName":"HighCPU"}',
        headers={"authorization": "secret"},
        alert_hash=alert_hash,
        dedup_key=alert_hash,
        ai_analysis=ai_analysis or {"importance": "high", "summary": "cpu"},
        importance="high",
        processing_status=processing_status,
        forward_status="pending",
        is_duplicate=is_duplicate,
        duplicate_of=duplicate_of,
        duplicate_count=1,
    )
    session.add(event)
    await session.flush()
    return event


@pytest.mark.asyncio
async def test_repository_duplicate_payload_context_and_suppressed_queries(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from core.compression import compress_payload
    from models import SuppressedRecord, WebhookEvent
    from services.webhooks import repository

    async with session_factory.begin() as session:
        old_original = await _insert_event(
            session,
            alert_hash="history",
            timestamp=utcnow() - timedelta(hours=48),
            ai_analysis={"importance": "medium", "summary": "old"},
        )
        recent_original = await _insert_event(session, alert_hash="hash-1", request_id="orig")
        recent_dup = await _insert_event(
            session,
            alert_hash="hash-1",
            request_id="dup",
            is_duplicate=True,
            duplicate_of=recent_original.id,
        )
        other_recent = await _insert_event(session, alert_hash="other", request_id="other")
        raw_only = WebhookEvent(
            source="prometheus",
            timestamp=utcnow(),
            raw_payload=compress_payload('{"from_raw": true}'),
            parsed_data=None,
            alert_hash="raw",
            processing_status=WebhookProcessingStatus.COMPLETED,
        )
        session.add(raw_only)
        session.add(
            SuppressedRecord(
                alert_hash="suppressed",
                source="prometheus",
                relation="derived",
                root_cause_event_id=recent_original.id,
                reason="same incident",
                related_alert_ids=[recent_dup.id],
                confidence=0.91,
                created_at=utcnow(),
            )
        )
        await session.flush()

    async with session_factory() as session:
        duplicate = await repository.check_duplicate_event("hash-1", session=session, time_window_hours=24)
        history = await repository.check_duplicate_event("history", session=session, time_window_hours=24)
        missing = await repository.check_duplicate_event("missing", session=session, time_window_hours=24)
        raw_payload_event = await session.get(WebhookEvent, raw_only.id)
        assert raw_payload_event is not None
        parsed, raw_text = await repository.load_event_payload(raw_payload_event)
        suppressed = await repository.list_suppressed_records(session, since_minutes=60, limit=999)
        count = await repository.count_suppressed_records(session, since_minutes=60)

    contexts = await repository.list_recent_alert_contexts("hash-1", utcnow(), 60)

    assert duplicate.is_duplicate is True
    assert duplicate.original_event is not None
    assert duplicate.original_event.id == recent_original.id
    assert history.is_duplicate is False
    assert history.original_event is not None
    assert history.original_event.id == old_original.id
    assert missing == repository.DuplicateCheckResult(False, None)
    assert parsed == {"from_raw": True}
    assert raw_text == '{"from_raw": true}'
    assert suppressed[0]["relation"] == "derived"
    assert count == 1
    assert any(ctx.event_id == other_recent.id for ctx in contexts)


@pytest.mark.asyncio
async def test_command_service_new_duplicate_existing_and_raw_payload_paths(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from models import WebhookEvent
    from services.webhooks import command_service

    assert command_service._stored_raw_payload(b"\xff") == b"\xff"

    async with session_factory.begin() as session:
        original = await _insert_event(
            session,
            alert_hash="dupe",
            ai_analysis={},
        )
        original.ai_analysis = {}
        duplicate_payload = command_service.SaveWebhookInput(
            data={"RuleName": "HighCPU"},
            source="prometheus",
            raw_payload=b'{"RuleName":"HighCPU"}',
            headers={"authorization": "Bearer secret"},
            request_id="dup-request",
            ai_analysis={"importance": "critical", "summary": "reanalyzed"},
            forward_status="queued",
            alert_hash="dupe",
            is_duplicate=True,
            original_event=original,
            reanalyzed=True,
        )
        duplicate = await command_service.save_webhook_data_in_session(session, input=duplicate_payload)
        assert duplicate.is_duplicate is True
        assert duplicate.original_id == original.id
        assert original.ai_analysis == {"importance": "critical", "summary": "reanalyzed"}

    async with session_factory.begin() as session:
        completed = await _insert_event(
            session,
            alert_hash="done",
            request_id="same-request",
            is_duplicate=True,
            duplicate_of=original.id,
        )
        completed_result = await command_service.save_webhook_data_in_session(
            session,
            input=command_service.SaveWebhookInput(
                data={"RuleName": "Completed"},
                source="prometheus",
                request_id="same-request",
                alert_hash="done",
            ),
        )
        assert completed_result.webhook_id == completed.id
        assert completed_result.is_duplicate is True

    async with session_factory.begin() as session:
        pending = await _insert_event(
            session,
            alert_hash="pending",
            request_id="pending-request",
            processing_status=WebhookProcessingStatus.RECEIVED,
        )
        updated = await command_service.save_webhook_data_in_session(
            session,
            input=command_service.SaveWebhookInput(
                data={"RuleName": "Updated"},
                source="prometheus",
                request_id="pending-request",
                alert_hash="new-hash",
                ai_analysis={"importance": "low", "summary": "updated"},
                forward_status="sent",
            ),
        )
        assert updated.webhook_id == pending.id
        refreshed = await session.get(WebhookEvent, pending.id)
        assert refreshed is not None
        assert refreshed.forward_status == "sent"
        assert refreshed.is_duplicate is False


@pytest.mark.asyncio
async def test_outbox_create_schedule_forward_list_and_mask_paths(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from services.forwarding import outbox
    from services.webhooks.decisioning import ForwardDecision

    async with session_factory.begin() as session:
        skipped = await outbox.resolve_and_forward(
            session=session,
            decision=ForwardDecision(False, "none", False, []),
            webhook_id=1,
            policy=_policy(),
        )
        ids = await outbox._create_outbox_records(
            session,
            [
                _rule(id=1, target_url=""),
                _rule(id=2, target_url="https://target.test/a/" + "x" * 80),
                _rule(id=3, target_type="openclaw", target_url=""),
            ],
            webhook_id=1,
            orig_id=None,
            forward_data={"source": "prometheus", "parsed_data": {"RuleName": "HighCPU"}},
            analysis_result={"importance": "high"},
            formatted_payload=None,
            event_type="alert",
            is_periodic_reminder=False,
            policy=_policy(),
            log_tag="test",
        )
        duplicate_ids = await outbox._create_outbox_records(
            session,
            [_rule(id=2, target_url="https://target.test/a/" + "x" * 80)],
            webhook_id=1,
            orig_id=None,
            forward_data={},
            analysis_result={},
            formatted_payload=None,
            event_type="alert",
            is_periodic_reminder=False,
            policy=_policy(),
            log_tag="test",
        )
        manual_retry_ids = await outbox._create_outbox_records(
            session,
            [_rule(id=2, target_url="https://target.test/a/" + "x" * 80)],
            webhook_id=1,
            orig_id=None,
            forward_data={},
            analysis_result={},
            formatted_payload=None,
            event_type="alert",
            is_periodic_reminder=False,
            idempotency_extra="manual-click-1",
            policy=_policy(),
            log_tag="test",
        )

    assert skipped == {"status": "skipped", "reason": "未匹配转发规则", "outbox_ids": []}
    assert len(ids) == 2
    assert duplicate_ids == [ids[0]]
    assert len(manual_retry_ids) == 1
    assert manual_retry_ids != [ids[0]]
    assert outbox._outbox_result([])["status"] == "skipped"
    assert outbox._mask_url_for_display("") == ""
    assert outbox._mask_url_for_display("not a url" * 20) == "***"

    scheduled: list[list[int]] = []
    delivered: list[int] = []

    async def schedule_many(outbox_ids: list[int]) -> None:
        scheduled.append(outbox_ids)

    async def deliver_one(outbox_id: int, **_kwargs: object) -> dict[str, object]:
        delivered.append(outbox_id)
        return {"status": "success"}

    monkeypatch.setattr(outbox, "schedule_forward_outbox_many", schedule_many)
    monkeypatch.setattr(outbox, "_deliver_one", deliver_one)

    async def no_rules() -> list[object]:
        return []

    monkeypatch.setattr("services.forwarding.rules.list_enabled_forward_rules", no_rules)

    queued = await outbox.forward_notification(
        event_type="alert",
        formatted_payload={"text": "hello"},
        webhook_id=10,
        target_url="https://direct.test/hook",
        policy=_policy(),
    )
    waited = await outbox.forward_notification(
        event_type="alert",
        formatted_payload={"text": "hello"},
        webhook_id=11,
        target_url="https://direct.test/wait",
        wait=True,
        policy=_policy(),
    )
    none = await outbox.forward_notification(
        event_type="alert",
        formatted_payload={},
        target_url="",
        policy=_policy(),
    )

    assert queued["status"] == "queued"
    assert scheduled
    assert waited["status"] == "success"
    assert delivered
    assert none["status"] == "skipped"

    clamped = await outbox.list_outbox_records(page=999, page_size=999, status=ForwardOutboxStatus.PENDING)
    listed = await outbox.list_outbox_records(page=1, page_size=999, status=ForwardOutboxStatus.PENDING)
    assert clamped["page"] == 100
    assert listed["page_size"] == 200
    assert listed["total"] >= 1
    assert listed["items"]


@pytest.mark.asyncio
async def test_outbox_delivery_finalize_failure_and_requeue_paths(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from models import DeepAnalysis, ForwardOutbox, WebhookEvent
    from services.forwarding import outbox

    scheduled_many: list[list[int]] = []
    scheduled_retry: list[tuple[int, int]] = []
    scheduled_openclaw: list[int] = []

    async def schedule_many(outbox_ids: list[int]) -> None:
        scheduled_many.append(outbox_ids)

    async def schedule_retry(outbox_id: int, delay_seconds: int) -> None:
        scheduled_retry.append((outbox_id, delay_seconds))

    async def schedule_poll(analysis_id: int) -> None:
        scheduled_openclaw.append(analysis_id)

    async def exhausted_notification(**_kwargs: object) -> dict[str, object]:
        return {"status": "queued"}

    monkeypatch.setattr(outbox, "schedule_forward_outbox_many", schedule_many)
    monkeypatch.setattr(outbox, "schedule_forward_outbox_retry", schedule_retry)
    monkeypatch.setattr(outbox, "forward_notification", exhausted_notification)
    monkeypatch.setattr("services.operations.taskiq_retry_scheduler.schedule_openclaw_poll_best_effort", schedule_poll)

    async with session_factory.begin() as session:
        event = await _insert_event(session, alert_hash="outbox")
        retry_record = ForwardOutbox(
            idempotency_key="retry",
            webhook_event_id=event.id,
            target_type="webhook",
            target_url="https://target.test/retry",
            channel_name="webhook",
            status=ForwardOutboxStatus.PROCESSING,
            attempts=1,
            max_attempts=3,
            created_at=utcnow(),
            next_attempt_at=utcnow(),
        )
        exhausted_record = ForwardOutbox(
            idempotency_key="exhausted",
            webhook_event_id=event.id,
            target_type="webhook",
            target_url="https://target.test/exhausted",
            channel_name="webhook",
            status=ForwardOutboxStatus.PROCESSING,
            attempts=3,
            max_attempts=3,
            created_at=utcnow(),
            next_attempt_at=utcnow(),
        )
        openclaw_record = ForwardOutbox(
            idempotency_key="openclaw",
            webhook_event_id=event.id,
            target_type="openclaw",
            target_url="",
            channel_name="openclaw",
            status=ForwardOutboxStatus.PROCESSING,
            attempts=1,
            max_attempts=2,
            forward_data={"source": "prometheus", "parsed_data": {"RuleName": "HighCPU"}},
            analysis_result={"importance": "high"},
            created_at=utcnow(),
            next_attempt_at=utcnow(),
        )
        terminal = ForwardOutbox(
            idempotency_key="terminal",
            webhook_event_id=event.id,
            target_type="webhook",
            status=ForwardOutboxStatus.SENT,
            attempts=1,
            max_attempts=2,
        )
        session.add_all([retry_record, exhausted_record, openclaw_record, terminal])
        await session.flush()
        retry_id = retry_record.id
        exhausted_id = exhausted_record.id
        openclaw_id = openclaw_record.id
        terminal_id = terminal.id

    await outbox._finalize_outbox_failure(retry_id, "temporary", policy=_policy())
    await outbox._finalize_outbox_failure(exhausted_id, "permanent", policy=_policy())
    await outbox._finalize_outbox_failure(terminal_id, "ignored", policy=_policy())

    async with session_factory() as session:
        retry_record = await session.get(ForwardOutbox, retry_id)
        exhausted_record = await session.get(ForwardOutbox, exhausted_id)
        assert retry_record is not None
        assert exhausted_record is not None
        assert retry_record.status == ForwardOutboxStatus.RETRYING
        assert exhausted_record.status == ForwardOutboxStatus.EXHAUSTED

    await outbox._finalize_outbox_success(
        ForwardOutbox(id=openclaw_id),
        {
            "status": "pending",
            "_pending": True,
            "_openclaw_run_id": "run-1",
            "_openclaw_session_key": "session-1",
        },
    )

    async with session_factory() as session:
        analyses = (await session.execute(select(DeepAnalysis))).scalars().all()
        event = (await session.execute(select(WebhookEvent).where(WebhookEvent.alert_hash == "outbox"))).scalar_one()
        assert analyses[0].status == DeepAnalysisStatus.PENDING
        assert event.forward_status == "sent"

    assert scheduled_retry == [(retry_id, 3)]
    assert scheduled_openclaw == [analyses[0].id]
    assert await outbox.requeue_forward_outbox(retry_id) is True
    assert await outbox.requeue_forward_outbox(999999) is False
    assert scheduled_many[-1] == [retry_id]


@pytest.mark.asyncio
async def test_data_maintenance_archives_policy_matched_events(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from models import ArchivedWebhookEvent, WebhookEvent
    from services.operations.data_maintenance import _days_threshold, cleanup_old_data_by_policy
    from services.operations.policies import DataMaintenancePolicy

    policy = DataMaintenancePolicy(
        enabled=True,
        retention_days_default=30,
        retention_policies={"high": 1},
        source_retention_policies={"prometheus": 2},
        cleanup_keywords={"parsed_data": ("cleanup-me", ""), "unknown": ("ignored",)},
    )
    now = utcnow()
    assert _days_threshold(now, -10) == now
    assert (
        await cleanup_old_data_by_policy(
            policy=DataMaintenancePolicy(False, 30, {}, {}, {}),
        )
        == 0
    )

    async with session_factory.begin() as session:
        high_old = await _insert_event(
            session,
            alert_hash="high-old",
            timestamp=now - timedelta(days=5),
        )
        source_old = await _insert_event(
            session,
            alert_hash="source-old",
            timestamp=now - timedelta(days=5),
        )
        source_old.importance = "low"
        keyword_old = await _insert_event(
            session,
            alert_hash="keyword-old",
            timestamp=now - timedelta(days=31),
        )
        keyword_old.source = "other"
        keyword_old.importance = "low"
        keyword_old.parsed_data = {"message": "cleanup-me"}
        keep_recent = await _insert_event(
            session,
            alert_hash="keep-recent",
            timestamp=now,
        )
        ids_to_archive = {high_old.id, source_old.id, keyword_old.id}

    archived = await cleanup_old_data_by_policy(policy=policy)
    no_more_archives = await cleanup_old_data_by_policy(policy=policy)

    async with session_factory() as session:
        remaining_ids = set((await session.scalars(select(WebhookEvent.id))).all())
        archived_ids = set((await session.scalars(select(ArchivedWebhookEvent.id))).all())

    assert archived == 3
    assert no_more_archives == 0
    assert ids_to_archive <= archived_ids
    assert keep_recent.id in remaining_ids

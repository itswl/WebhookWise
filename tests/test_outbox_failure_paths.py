"""Tests for outbox failure, retry exhaustion, stale claim handling, and claim semantics.

Reuses the SQLite session-factory pattern from test_forward_outbox.py.
"""

from collections.abc import AsyncIterator
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

from services.forwarding.policies import ForwardDeliveryPolicy
from services.webhooks.types import DeepAnalysisStatus, ForwardOutboxStatus


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
    context = AppContext()
    context.db_engine = engine
    context.session_factory = factory
    set_default_app_context(context)

    yield factory
    set_default_app_context(None)
    await engine.dispose()


# ── helpers ──────────────────────────────────────────────────────────


def _outbox_policy(*, stale_processing_threshold_seconds: int = 60) -> ForwardDeliveryPolicy:
    return ForwardDeliveryPolicy(
        timeout_seconds=10,
        max_attempts=3,
        retry_initial_delay=1,
        retry_max_delay=10,
        retry_backoff_multiplier=2.0,
        stale_processing_threshold_seconds=stale_processing_threshold_seconds,
        max_delivery_age_seconds=1800,
    )


async def _insert_outbox(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    webhook_event_id: int = 1,
    original_event_id: int | None = None,
    status: str = ForwardOutboxStatus.PENDING,
    attempts: int = 0,
    max_attempts: int = 3,
    target_type: str = "webhook",
    next_attempt_at: datetime | None = None,
    updated_at: datetime | None = None,
    created_at: datetime | None = None,
) -> int:
    from models import ForwardOutbox

    now = datetime.now()
    async with session_factory.begin() as session:
        record = ForwardOutbox(
            idempotency_key=f"forward:test-{now.timestamp()}",
            webhook_event_id=webhook_event_id,
            original_event_id=original_event_id,
            target_type=target_type,
            target_url="https://example.test/hook",
            status=status,
            attempts=attempts,
            max_attempts=max_attempts,
            next_attempt_at=next_attempt_at or now,
            forward_data={"source": "test"},
            analysis_result={"summary": "x"},
            created_at=created_at or now,
            updated_at=updated_at or now,
        )
        session.add(record)
        await session.flush()
        return record.id


# ── _is_forward_success ──────────────────────────────────────────────


class TestIsForwardSuccess:
    def test_success_status(self) -> None:
        from services.forwarding.outbox import _is_forward_success

        assert _is_forward_success({"status": "success"}) is True

    def test_pending_flag(self) -> None:
        from services.forwarding.outbox import _is_forward_success

        assert _is_forward_success({"_pending": True, "status": "other"}) is True

    def test_failed_status(self) -> None:
        from services.forwarding.outbox import _is_forward_success

        assert _is_forward_success({"status": "failed"}) is False

    def test_empty_dict(self) -> None:
        from services.forwarding.outbox import _is_forward_success

        assert _is_forward_success({}) is False


# ── _claim_outbox ────────────────────────────────────────────────────


class TestClaimOutbox:
    async def test_claims_pending_with_past_attempt_at(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        from services.forwarding.outbox import _claim_outbox

        outbox_id = await _insert_outbox(session_factory, next_attempt_at=datetime.now() - timedelta(seconds=10))
        record = await _claim_outbox(outbox_id)
        assert record is not None
        assert record.status == ForwardOutboxStatus.PROCESSING
        assert record.attempts == 1

    async def test_returns_none_for_sent_status(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        from services.forwarding.outbox import _claim_outbox

        outbox_id = await _insert_outbox(session_factory, status=ForwardOutboxStatus.SENT)
        record = await _claim_outbox(outbox_id)
        assert record is None

    async def test_returns_none_for_future_attempt_at(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        from services.forwarding.outbox import _claim_outbox

        outbox_id = await _insert_outbox(session_factory, next_attempt_at=datetime.now() + timedelta(hours=1))
        record = await _claim_outbox(outbox_id)
        assert record is None

    async def test_expires_old_pending_record(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        from models import ForwardOutbox
        from services.forwarding.outbox import _claim_outbox

        created_at = datetime.now() - timedelta(minutes=31)
        outbox_id = await _insert_outbox(
            session_factory,
            next_attempt_at=datetime.now() - timedelta(seconds=1),
            created_at=created_at,
            updated_at=created_at,
        )

        record = await _claim_outbox(outbox_id, policy=_outbox_policy(stale_processing_threshold_seconds=60))

        async with session_factory() as session:
            updated = await session.get(ForwardOutbox, outbox_id)
        assert record is None
        assert updated is not None
        assert updated.status == ForwardOutboxStatus.EXPIRED

    async def test_claims_retrying_status(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        from services.forwarding.outbox import _claim_outbox

        outbox_id = await _insert_outbox(
            session_factory, status=ForwardOutboxStatus.RETRYING, next_attempt_at=datetime.now() - timedelta(seconds=1)
        )
        record = await _claim_outbox(outbox_id)
        assert record is not None
        assert record.status == ForwardOutboxStatus.PROCESSING


# ── _finalize_outbox_failure ─────────────────────────────────────────


class TestFinalizeOutboxFailure:
    async def test_transitions_to_retrying(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from models import ForwardOutbox
        from services.forwarding.outbox import _claim_outbox, _finalize_outbox_failure

        async def _noop(*_: object, **__: object) -> None:
            pass

        monkeypatch.setattr("services.forwarding.outbox.schedule_forward_outbox_retry", _noop)

        outbox_id = await _insert_outbox(
            session_factory, attempts=0, max_attempts=3, next_attempt_at=datetime.now() - timedelta(seconds=1)
        )
        await _claim_outbox(outbox_id)
        await _finalize_outbox_failure(outbox_id, "test error")

        async with session_factory() as session:
            record = await session.get(ForwardOutbox, outbox_id)
        assert record is not None
        assert record.status == ForwardOutboxStatus.RETRYING
        assert record.next_attempt_at is not None
        assert record.next_attempt_at > datetime.now() - timedelta(seconds=1)

    async def test_transitions_to_exhausted_at_max_attempts(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from models import ForwardOutbox
        from services.forwarding.outbox import _claim_outbox, _finalize_outbox_failure
        from services.webhooks.types import ForwardOutboxStatus

        async def _noop(*_: object, **__: object) -> None:
            pass

        monkeypatch.setattr("services.forwarding.outbox.schedule_forward_outbox_retry", _noop)
        enqueued: list[dict[str, object]] = []

        async def fake_forward_notification(**kwargs: object) -> dict[str, object]:
            enqueued.append(dict(kwargs))
            return {"status": "queued", "outbox_id": 1}

        monkeypatch.setattr("services.forwarding.outbox.forward_notification", fake_forward_notification)

        outbox_id = await _insert_outbox(
            session_factory, attempts=2, max_attempts=3, next_attempt_at=datetime.now() - timedelta(seconds=1)
        )
        await _claim_outbox(outbox_id)  # attempts becomes 3
        await _finalize_outbox_failure(outbox_id, "exhausted")

        async with session_factory() as session:
            record = await session.get(ForwardOutbox, outbox_id)
            assert record is not None
            assert record.status == ForwardOutboxStatus.EXHAUSTED
        assert len(enqueued) == 1


class TestRequeueOutbox:
    async def test_requeue_exhausted_outbox_resets_attempts(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from models import ForwardOutbox
        from services.forwarding.outbox import requeue_forward_outbox
        from services.webhooks.types import ForwardOutboxStatus

        async def _noop(*_: object, **__: object) -> None:
            pass

        monkeypatch.setattr("services.forwarding.outbox.schedule_forward_outbox_many", _noop)

        outbox_id = await _insert_outbox(
            session_factory, attempts=2, max_attempts=3, next_attempt_at=datetime.now() - timedelta(seconds=1)
        )
        async with session_factory.begin() as session:
            record = await session.get(ForwardOutbox, outbox_id)
            assert record is not None
            record.status = ForwardOutboxStatus.EXHAUSTED

        assert await requeue_forward_outbox(outbox_id) is True

        async with session_factory() as session:
            updated = await session.get(ForwardOutbox, outbox_id)
            assert updated is not None
            assert updated.status == ForwardOutboxStatus.RETRYING
            assert updated.attempts == 0


# ── _finalize_outbox_success ─────────────────────────────────────────


class TestFinalizeOutboxSuccess:
    async def test_sets_sent_status(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from models import ForwardOutbox
        from services.forwarding.outbox import _claim_outbox, _finalize_outbox_success

        async def _noop(*_: object) -> None:
            pass

        monkeypatch.setattr("services.operations.taskiq_retry_scheduler.schedule_openclaw_poll_best_effort", _noop)

        outbox_id = await _insert_outbox(session_factory, next_attempt_at=datetime.now() - timedelta(seconds=1))
        record = await _claim_outbox(outbox_id)
        assert record is not None
        await _finalize_outbox_success(record, {"status": "success", "status_code": 200})

        async with session_factory() as session:
            updated = await session.get(ForwardOutbox, outbox_id)
        assert updated is not None
        assert updated.status == ForwardOutboxStatus.SENT
        assert updated.sent_at is not None

    async def test_marks_current_and_original_events_sent(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from models import WebhookEvent
        from services.forwarding.outbox import _claim_outbox, _finalize_outbox_success

        async def _noop(*_: object) -> None:
            pass

        monkeypatch.setattr("services.operations.taskiq_retry_scheduler.schedule_openclaw_poll_best_effort", _noop)

        async with session_factory.begin() as session:
            original = WebhookEvent(
                source="volcengine",
                request_id="orig-1",
                forward_status="queued",
                is_duplicate=False,
            )
            duplicate = WebhookEvent(
                source="volcengine",
                request_id="dup-1",
                forward_status="queued",
                is_duplicate=True,
            )
            session.add_all([original, duplicate])
            await session.flush()
            original_id = original.id
            duplicate_id = duplicate.id

        outbox_id = await _insert_outbox(
            session_factory,
            webhook_event_id=duplicate_id,
            original_event_id=original_id,
            next_attempt_at=datetime.now() - timedelta(seconds=1),
        )
        record = await _claim_outbox(outbox_id)
        assert record is not None
        await _finalize_outbox_success(record, {"status": "success", "status_code": 200})

        async with session_factory() as session:
            original = await session.get(WebhookEvent, original_id)
            duplicate = await session.get(WebhookEvent, duplicate_id)

        assert original is not None
        assert duplicate is not None
        assert original.forward_status == "sent"
        assert duplicate.forward_status == "sent"
        assert original.last_notified_at is not None
        assert duplicate.last_notified_at is not None

    async def test_creates_deep_analysis_for_openclaw(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from models import DeepAnalysis
        from services.forwarding.outbox import _claim_outbox, _finalize_outbox_success

        async def _noop(*_: object) -> None:
            pass

        monkeypatch.setattr("services.operations.taskiq_retry_scheduler.schedule_openclaw_poll_best_effort", _noop)

        outbox_id = await _insert_outbox(
            session_factory,
            webhook_event_id=37769,
            original_event_id=37291,
            target_type="openclaw",
            next_attempt_at=datetime.now() - timedelta(seconds=1),
        )
        record = await _claim_outbox(outbox_id)
        assert record is not None
        result = {"_pending": True, "_openclaw_run_id": "run-1", "_openclaw_session_key": "key-1"}
        await _finalize_outbox_success(record, result)

        async with session_factory() as session:
            deep = (await session.execute(select(DeepAnalysis))).scalars().first()
        assert deep is not None
        assert deep.webhook_event_id == 37769
        assert deep.status == DeepAnalysisStatus.PENDING
        assert deep.openclaw_run_id == "run-1"


# ── run_forward_outbox_scan ──────────────────────────────────────────


class TestRunForwardOutboxScan:
    async def test_recovers_stale_processing(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from models import ForwardOutbox
        from services.forwarding.outbox_scanner import run_forward_outbox_scan

        scheduled_ids: list[list[int]] = []

        async def _fake_schedule(ids: list[int]) -> None:
            scheduled_ids.append(ids)

        monkeypatch.setattr("services.forwarding.outbox.schedule_forward_outbox_many", _fake_schedule)

        stale_time = datetime.now() - timedelta(hours=1)
        await _insert_outbox(
            session_factory,
            status=ForwardOutboxStatus.PROCESSING,
            updated_at=stale_time,
            next_attempt_at=stale_time,
        )

        await run_forward_outbox_scan(policy=_outbox_policy(stale_processing_threshold_seconds=60))

        async with session_factory() as session:
            record = (await session.execute(select(ForwardOutbox))).scalar_one()
        assert record.status == ForwardOutboxStatus.RETRYING
        assert scheduled_ids  # was scheduled for retry

    async def test_selects_due_pending_rows(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from services.forwarding.outbox_scanner import run_forward_outbox_scan

        scheduled_ids: list[list[int]] = []

        async def _fake_schedule(ids: list[int]) -> None:
            scheduled_ids.append(ids)

        monkeypatch.setattr("services.forwarding.outbox.schedule_forward_outbox_many", _fake_schedule)

        await _insert_outbox(session_factory, next_attempt_at=datetime.now() - timedelta(seconds=10))

        await run_forward_outbox_scan(policy=_outbox_policy(stale_processing_threshold_seconds=60))

        assert scheduled_ids
        assert len(scheduled_ids[0]) == 1

    async def test_expires_old_rows_before_scheduling(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from models import ForwardOutbox
        from services.forwarding.outbox_scanner import run_forward_outbox_scan

        scheduled_ids: list[list[int]] = []

        async def _fake_schedule(ids: list[int]) -> None:
            scheduled_ids.append(ids)

        monkeypatch.setattr("services.forwarding.outbox.schedule_forward_outbox_many", _fake_schedule)

        old = datetime.now() - timedelta(hours=1)
        await _insert_outbox(
            session_factory,
            next_attempt_at=datetime.now() - timedelta(seconds=10),
            created_at=old,
            updated_at=old,
        )

        count = await run_forward_outbox_scan(policy=_outbox_policy(stale_processing_threshold_seconds=60))

        async with session_factory() as session:
            record = (await session.execute(select(ForwardOutbox))).scalar_one()
        assert count == 1
        assert scheduled_ids == [[]]
        assert record.status == ForwardOutboxStatus.EXPIRED


# ── OpenClaw poll claim / stability ───────────────────────────────────


class TestOpenClawPoller:
    @pytest.fixture(autouse=True)
    def _bind_config(self, temp_config) -> None:
        self.config = temp_config

    async def test_poll_openclaw_result_via_http_uses_configured_timeout_and_waits_for_final_text(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from services.analysis import openclaw

        seen_timeouts: list[tuple[float, float]] = []

        class _Response:
            status_code = 200

            def json(self) -> dict[str, object]:
                return {
                    "isProcessing": False,
                    "isFinal": False,
                    "text": "partial result",
                    "messageCount": 1,
                }

        class _Client:
            async def get(self, *_: object, **kwargs: object) -> _Response:
                timeout = kwargs["timeout"]
                seen_timeouts.append((float(timeout.connect), float(timeout.read)))
                assert kwargs["headers"]["Connection"] == "close"
                return _Response()

        monkeypatch.setattr(self.config.openclaw, "OPENCLAW_POLL_TIMEOUT", 7)
        monkeypatch.setattr(self.config.openclaw, "OPENCLAW_CONNECT_TIMEOUT", 3)
        monkeypatch.setattr(self.config.openclaw, "OPENCLAW_HTTP_API_URL", "http://openclaw.test")
        monkeypatch.setattr(openclaw, "get_http_client", lambda: _Client())

        result = await openclaw.poll_openclaw_result_via_http("session-1", retry_count=1)

        assert result["status"] == "pending"
        assert seen_timeouts == [(3.0, 7.0)]

    async def test_poll_openclaw_result_via_http_marks_transport_errors_retryable(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from services.analysis import openclaw

        class _Client:
            async def get(self, *_: object, **__: object) -> object:
                raise OSError("All connection attempts failed")

        monkeypatch.setattr(self.config.openclaw, "OPENCLAW_HTTP_API_URL", "http://openclaw.test")
        monkeypatch.setattr(openclaw, "get_http_client", lambda: _Client())

        result = await openclaw.poll_openclaw_result_via_http("session-1", retry_count=1)

        assert result["status"] == "error"
        assert result["retryable"] is True
        assert "connection attempts" in str(result["error"]).lower()

    async def test_poll_openclaw_result_via_http_treats_read_timeout_as_pending(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import httpx

        from services.analysis import openclaw

        class _Client:
            async def get(self, *_: object, **__: object) -> object:
                raise httpx.ReadTimeout("")

        monkeypatch.setattr(self.config.openclaw, "OPENCLAW_HTTP_API_URL", "http://openclaw.test")
        monkeypatch.setattr(self.config.openclaw, "OPENCLAW_POLL_TIMEOUT", 7)
        monkeypatch.setattr(openclaw, "get_http_client", lambda: _Client())

        result = await openclaw.poll_openclaw_result_via_http("session-1", retry_count=1)

        assert result["status"] == "pending"
        assert "ReadTimeout" in str(result["error"])

    async def test_poll_openclaw_result_via_http_invalid_json_is_terminal_upstream_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from services.analysis import openclaw

        class _Response:
            status_code = 200

            def json(self) -> dict[str, object]:
                raise ValueError("not json")

        class _Client:
            async def get(self, *_: object, **__: object) -> _Response:
                return _Response()

        monkeypatch.setattr(self.config.openclaw, "OPENCLAW_HTTP_API_URL", "http://openclaw.test")
        monkeypatch.setattr(openclaw, "get_http_client", lambda: _Client())

        result = await openclaw.poll_openclaw_result_via_http("session-1", retry_count=1)

        assert result["status"] == "error"
        assert result.get("retryable") is not True
        assert result["error"] == "Invalid JSON response"

    def test_poll_claim_lease_scales_with_http_poll_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from services.analysis.openclaw import _poll_claim_lease_seconds

        monkeypatch.setattr(self.config.openclaw, "OPENCLAW_POLL_TIMEOUT", 120)

        assert _poll_claim_lease_seconds() == 390

    async def test_claim_openclaw_poll_sets_inflight_lease(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from models import DeepAnalysis
        from services.analysis.openclaw import _claim_openclaw_poll

        async with session_factory.begin() as session:
            record = DeepAnalysis(
                webhook_event_id=1,
                engine="openclaw",
                status=DeepAnalysisStatus.PENDING,
                openclaw_session_key="session-1",
                openclaw_run_id="run-1",
                next_poll_at=datetime.now() - timedelta(seconds=1),
                poll_attempts=0,
            )
            session.add(record)
            await session.flush()
            analysis_id = record.id

        claimed, early_delay = await _claim_openclaw_poll(analysis_id)
        assert claimed is not None
        assert early_delay is None
        assert claimed["poll_attempts"] == 1

        claimed_again, second_delay = await _claim_openclaw_poll(analysis_id)
        assert claimed_again is None
        assert second_delay is not None
        assert second_delay > 0

        async with session_factory() as session:
            updated = await session.get(DeepAnalysis, analysis_id)
        assert updated is not None
        assert updated.next_poll_at is not None
        assert updated.next_poll_at > datetime.now()

    async def test_poll_single_record_completes_immediately_when_stability_hits_is_one(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from services.analysis import openclaw

        monkeypatch.setattr(self.config.openclaw, "OPENCLAW_STABILITY_REQUIRED_HITS", 1)
        monkeypatch.setattr(self.config.openclaw, "OPENCLAW_HTTP_API_URL", "http://openclaw.test")

        async def _completed(*_: object, **__: object) -> dict[str, object]:
            return {"status": "completed", "text": "root cause ready", "msg_count": 2}

        monkeypatch.setattr(openclaw, "poll_openclaw_result_via_http", _completed)

        result = await openclaw._poll_single_record(
            {
                "id": 1,
                "webhook_event_id": 1,
                "engine": "openclaw",
                "openclaw_session_key": "session-1",
                "openclaw_run_id": "run-1",
                "created_at": datetime.now(),
                "status": DeepAnalysisStatus.PENDING,
                "analysis_result": None,
                "duration_seconds": 0,
            }
        )

        assert result["action"] == "update"
        assert result["status"] == DeepAnalysisStatus.COMPLETED
        assert result["_need_success_notify"] is True
        assert result["analysis_result"]["root_cause"] == "root cause ready"

    async def test_poll_single_record_trusts_explicit_final_http_result(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from services.analysis import openclaw

        monkeypatch.setattr(self.config.openclaw, "OPENCLAW_STABILITY_REQUIRED_HITS", 2)
        monkeypatch.setattr(self.config.openclaw, "OPENCLAW_HTTP_API_URL", "http://openclaw.test")

        async def _completed(*_: object, **__: object) -> dict[str, object]:
            return {"status": "completed", "text": "explicit final", "msg_count": 2, "is_final": True}

        async def _stability_should_not_be_written(*_: object, **__: object) -> None:
            raise AssertionError("explicit HTTP final should not need a stability snapshot")

        monkeypatch.setattr(openclaw, "poll_openclaw_result_via_http", _completed)
        monkeypatch.setattr(openclaw, "_set_poll_stability", _stability_should_not_be_written)

        result = await openclaw._poll_single_record(
            {
                "id": 1,
                "webhook_event_id": 1,
                "engine": "openclaw",
                "openclaw_session_key": "session-1",
                "openclaw_run_id": "run-1",
                "created_at": datetime.now(),
                "status": DeepAnalysisStatus.PENDING,
                "analysis_result": None,
                "duration_seconds": 0,
            }
        )

        assert result["action"] == "update"
        assert result["status"] == DeepAnalysisStatus.COMPLETED
        assert result["analysis_result"]["root_cause"] == "explicit final"

    async def test_poll_single_record_uses_manual_retry_time_for_timeout(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from services.analysis import openclaw

        monkeypatch.setattr(self.config.openclaw, "OPENCLAW_STABILITY_REQUIRED_HITS", 1)
        monkeypatch.setattr(self.config.openclaw, "OPENCLAW_HTTP_API_URL", "http://openclaw.test")
        monkeypatch.setattr(self.config.openclaw, "OPENCLAW_TIMEOUT_SECONDS", 900)

        async def _completed(*_: object, **__: object) -> dict[str, object]:
            return {"status": "completed", "text": "manual retry ready", "msg_count": 2}

        monkeypatch.setattr(openclaw, "poll_openclaw_result_via_http", _completed)

        result = await openclaw._poll_single_record(
            {
                "id": 1,
                "webhook_event_id": 1,
                "engine": "openclaw",
                "openclaw_session_key": "session-1",
                "openclaw_run_id": "run-1",
                "created_at": datetime.now() - timedelta(hours=2),
                "status": DeepAnalysisStatus.PENDING,
                "analysis_result": {
                    openclaw.MANUAL_RETRY_STARTED_AT_KEY: datetime.now().isoformat(),
                },
                "duration_seconds": 0,
            }
        )

        assert result["action"] == "update"
        assert result["status"] == DeepAnalysisStatus.COMPLETED
        assert result["analysis_result"]["root_cause"] == "manual retry ready"

    async def test_poll_single_record_keeps_pending_and_does_not_use_gateway_when_http_api_is_configured(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from services.analysis import openclaw

        monkeypatch.setattr(self.config.openclaw, "OPENCLAW_HTTP_API_URL", "http://openclaw.test")
        monkeypatch.setattr(self.config.openclaw, "OPENCLAW_GATEWAY_URL", "http://openclaw-gateway.test")

        async def _http_error(*_: object, **__: object) -> dict[str, object]:
            return {"status": "error", "error": "All connection attempts failed", "retryable": True}

        async def _gateway_should_not_be_called(*_: object, **__: object) -> dict[str, object]:
            raise AssertionError("configured OPENCLAW_HTTP_API_URL must be the only poll transport")

        monkeypatch.setattr(openclaw, "poll_openclaw_result_via_http", _http_error)
        monkeypatch.setattr(openclaw, "poll_session_result", _gateway_should_not_be_called)

        result = await openclaw._poll_single_record(
            {
                "id": 1,
                "webhook_event_id": 1,
                "engine": "openclaw",
                "openclaw_session_key": "session-1",
                "openclaw_run_id": "run-1",
                "created_at": datetime.now(),
                "status": DeepAnalysisStatus.PENDING,
                "analysis_result": None,
                "duration_seconds": 0,
            }
        )

        assert result == {"id": 1, "action": "skip"}

    async def test_poll_single_record_uses_gateway_when_http_api_is_not_configured(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from services.analysis import openclaw

        monkeypatch.setattr(self.config.openclaw, "OPENCLAW_STABILITY_REQUIRED_HITS", 1)
        monkeypatch.setattr(self.config.openclaw, "OPENCLAW_HTTP_API_URL", "")
        monkeypatch.setattr(self.config.openclaw, "OPENCLAW_GATEWAY_URL", "http://openclaw-gateway.test")

        async def _http_should_not_be_called(*_: object, **__: object) -> dict[str, object]:
            raise AssertionError("HTTP poll should be skipped when OPENCLAW_HTTP_API_URL is empty")

        async def _gateway_completed(*_: object, **__: object) -> dict[str, object]:
            return {"status": "completed", "text": "gateway result", "msg_count": 2}

        monkeypatch.setattr(openclaw, "poll_openclaw_result_via_http", _http_should_not_be_called)
        monkeypatch.setattr(openclaw, "poll_session_result", _gateway_completed)

        result = await openclaw._poll_single_record(
            {
                "id": 1,
                "webhook_event_id": 1,
                "engine": "openclaw",
                "openclaw_session_key": "session-1",
                "openclaw_run_id": "run-1",
                "created_at": datetime.now(),
                "status": DeepAnalysisStatus.PENDING,
                "analysis_result": None,
                "duration_seconds": 0,
            }
        )

        assert result["action"] == "update"
        assert result["status"] == DeepAnalysisStatus.COMPLETED
        assert result["analysis_result"]["root_cause"] == "gateway result"

    async def test_poll_single_record_treats_same_length_different_text_as_changed(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from services.analysis import openclaw

        stability_state: dict[str, object] = {
            "msg_count": 2,
            "text_len": 4,
            "text_hash": openclaw._text_hash("aaaa"),
            "hit_count": 1,
        }
        saved_states: list[dict[str, object]] = []
        cleared = {"value": False}

        async def _completed(*_: object, **__: object) -> dict[str, object]:
            return {"status": "completed", "text": "bbbb", "msg_count": 2}

        async def _get_stability(_: int) -> dict[str, object]:
            return stability_state

        async def _set_stability(_: int, data: dict[str, object]) -> None:
            saved_states.append(data)

        async def _clear(_: int) -> None:
            cleared["value"] = True

        monkeypatch.setattr(self.config.openclaw, "OPENCLAW_STABILITY_REQUIRED_HITS", 2)
        monkeypatch.setattr(self.config.openclaw, "OPENCLAW_HTTP_API_URL", "http://openclaw.test")
        monkeypatch.setattr(openclaw, "poll_openclaw_result_via_http", _completed)
        monkeypatch.setattr(openclaw, "_get_poll_stability", _get_stability)
        monkeypatch.setattr(openclaw, "_set_poll_stability", _set_stability)
        monkeypatch.setattr(openclaw, "_clear_poll_stability", _clear)

        result = await openclaw._poll_single_record(
            {
                "id": 1,
                "webhook_event_id": 1,
                "engine": "openclaw",
                "openclaw_session_key": "session-1",
                "openclaw_run_id": "run-1",
                "created_at": datetime.now(),
                "status": DeepAnalysisStatus.PENDING,
                "analysis_result": None,
                "duration_seconds": 0,
            }
        )

        assert result["action"] == "skip"
        assert saved_states[0]["text_hash"] == openclaw._text_hash("bbbb")
        assert cleared["value"] is False

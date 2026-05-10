"""Tests for outbox failure, retry exhaustion, stale recovery, and claim semantics.

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

from services.webhooks.types import DeepAnalysisStatus, ForwardOutboxStatus


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


# ── helpers ──────────────────────────────────────────────────────────


async def _insert_outbox(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    status: str = ForwardOutboxStatus.PENDING,
    attempts: int = 0,
    max_attempts: int = 3,
    target_type: str = "webhook",
    next_attempt_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> int:
    from models import ForwardOutbox

    now = datetime.now()
    async with session_factory.begin() as session:
        record = ForwardOutbox(
            idempotency_key=f"forward:test-{now.timestamp()}",
            webhook_event_id=1,
            target_type=target_type,
            target_url="https://example.test/hook",
            status=status,
            attempts=attempts,
            max_attempts=max_attempts,
            next_attempt_at=next_attempt_at or now,
            forward_data={"source": "test"},
            analysis_result={"summary": "x"},
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
    async def test_claims_pending_with_past_attempt_at(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        from services.forwarding.outbox import _claim_outbox

        outbox_id = await _insert_outbox(session_factory, next_attempt_at=datetime.now() - timedelta(seconds=10))
        record = await _claim_outbox(outbox_id)
        assert record is not None
        assert record.status == ForwardOutboxStatus.PROCESSING
        assert record.attempts == 1

    async def test_returns_none_for_sent_status(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        from services.forwarding.outbox import _claim_outbox

        outbox_id = await _insert_outbox(session_factory, status=ForwardOutboxStatus.SENT)
        record = await _claim_outbox(outbox_id)
        assert record is None

    async def test_returns_none_for_future_attempt_at(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        from services.forwarding.outbox import _claim_outbox

        outbox_id = await _insert_outbox(session_factory, next_attempt_at=datetime.now() + timedelta(hours=1))
        record = await _claim_outbox(outbox_id)
        assert record is None

    async def test_claims_retrying_status(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
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
        from models import FailedForward, ForwardOutbox
        from services.forwarding.outbox import _claim_outbox, _finalize_outbox_failure

        async def _noop(*_: object, **__: object) -> None:
            pass

        monkeypatch.setattr("services.forwarding.outbox.schedule_forward_outbox_retry", _noop)

        outbox_id = await _insert_outbox(
            session_factory, attempts=2, max_attempts=3, next_attempt_at=datetime.now() - timedelta(seconds=1)
        )
        await _claim_outbox(outbox_id)  # attempts becomes 3
        await _finalize_outbox_failure(outbox_id, "exhausted")

        async with session_factory() as session:
            record = await session.get(ForwardOutbox, outbox_id)
            assert record is not None
            assert record.status == ForwardOutboxStatus.EXHAUSTED

            failed = (await session.execute(select(FailedForward))).scalars().first()
            assert failed is not None
            assert failed.status == "exhausted"


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

        monkeypatch.setattr("services.forwarding.outbox._schedule_openclaw_poll_best_effort", _noop)

        outbox_id = await _insert_outbox(
            session_factory, next_attempt_at=datetime.now() - timedelta(seconds=1)
        )
        record = await _claim_outbox(outbox_id)
        assert record is not None
        await _finalize_outbox_success(record, {"status": "success", "status_code": 200})

        async with session_factory() as session:
            updated = await session.get(ForwardOutbox, outbox_id)
        assert updated is not None
        assert updated.status == ForwardOutboxStatus.SENT
        assert updated.sent_at is not None

    async def test_creates_deep_analysis_for_openclaw(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from models import DeepAnalysis
        from services.forwarding.outbox import _claim_outbox, _finalize_outbox_success

        async def _noop(*_: object) -> None:
            pass

        monkeypatch.setattr("services.forwarding.outbox._schedule_openclaw_poll_best_effort", _noop)

        outbox_id = await _insert_outbox(
            session_factory,
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
        from services.forwarding.outbox import run_forward_outbox_scan

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

        monkeypatch.setattr("services.forwarding.outbox.Config.server.RECOVERY_POLLER_STUCK_THRESHOLD_SECONDS", 60)
        await run_forward_outbox_scan()

        async with session_factory() as session:
            record = (await session.execute(select(ForwardOutbox))).scalar_one()
        assert record.status == ForwardOutboxStatus.RETRYING
        assert scheduled_ids  # was scheduled for retry

    async def test_selects_due_pending_rows(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from services.forwarding.outbox import run_forward_outbox_scan

        scheduled_ids: list[list[int]] = []

        async def _fake_schedule(ids: list[int]) -> None:
            scheduled_ids.append(ids)

        monkeypatch.setattr("services.forwarding.outbox.schedule_forward_outbox_many", _fake_schedule)

        await _insert_outbox(session_factory, next_attempt_at=datetime.now() - timedelta(seconds=10))

        monkeypatch.setattr("services.forwarding.outbox.Config.server.RECOVERY_POLLER_STUCK_THRESHOLD_SECONDS", 60)
        await run_forward_outbox_scan()

        assert scheduled_ids
        assert len(scheduled_ids[0]) == 1

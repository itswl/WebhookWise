from datetime import datetime, timedelta

import pytest
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles

from core.datetime_utils import utcnow

pytest.importorskip("fastapi")


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


@pytest.fixture()
async def session(monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    # Import models to register them with Base.metadata
    import models  # noqa: F401
    from db.session import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async with Session() as s:
        yield s
        await s.rollback()
    await engine.dispose()


async def test_webhooks_cursor_prev_alert_timestamp(session):
    from models import WebhookEvent
    from services.webhooks.query_service import list_webhook_summaries

    t0 = datetime(2026, 1, 1, 0, 0, 0)
    t1 = datetime(2026, 1, 1, 0, 1, 0)

    e1 = WebhookEvent(
        source="test",
        client_ip="127.0.0.1",
        timestamp=t0,
        importance="high",
        processing_status="completed",
        is_duplicate=False,
        duplicate_of=None,
        duplicate_count=1,
        prev_alert_id=None,
    )
    e2 = WebhookEvent(
        source="test",
        client_ip="127.0.0.2",
        timestamp=t1,
        importance="high",
        processing_status="completed",
        is_duplicate=True,
        duplicate_of=1,
        duplicate_count=2,
        prev_alert_id=1,
    )
    session.add_all([e1, e2])
    await session.commit()

    items, has_more, next_cursor = await list_webhook_summaries(page_size=200, session=session)
    assert isinstance(items, list)
    assert len(items) == 2

    newest = items[0]
    assert newest["id"] == 2
    assert newest["prev_alert_id"] == 1

    oldest = items[1]
    assert oldest["id"] == 1
    assert oldest["prev_alert_id"] is None


async def test_webhook_summary_uses_sent_outbox_status_for_duplicate(session):
    from models import ForwardOutbox, WebhookEvent
    from services.webhooks.query_service import list_webhook_summaries

    original = WebhookEvent(
        source="test",
        timestamp=datetime(2026, 1, 1, 0, 0, 0),
        importance="high",
        processing_status="completed",
        forward_status="queued",
        is_duplicate=False,
        duplicate_count=1,
    )
    duplicate = WebhookEvent(
        source="test",
        timestamp=datetime(2026, 1, 1, 0, 1, 0),
        importance="high",
        processing_status="completed",
        forward_status="queued",
        is_duplicate=True,
        duplicate_count=2,
    )
    session.add_all([original, duplicate])
    await session.flush()
    duplicate.duplicate_of = original.id
    outbox = ForwardOutbox(
        idempotency_key="forward:summary-status",
        webhook_event_id=duplicate.id,
        original_event_id=original.id,
        target_type="webhook",
        status="sent",
        attempts=1,
        max_attempts=3,
    )
    session.add(outbox)
    await session.commit()

    items, _, _ = await list_webhook_summaries(page_size=200, session=session)
    status_by_id = {item["id"]: item["forward_status"] for item in items}

    assert status_by_id[original.id] == "sent"
    assert status_by_id[duplicate.id] == "sent"


async def test_deep_analyses_list_fields(session, monkeypatch):
    from api.deep_analysis import list_all_deep_analyses
    from models import DeepAnalysis, WebhookEvent

    event = WebhookEvent(
        source="prometheus",
        client_ip="127.0.0.1",
        timestamp=datetime(2026, 1, 1, 0, 0, 0),
        importance="medium",
        processing_status="completed",
        is_duplicate=True,
        duplicate_of=1,
        duplicate_count=2,
        prev_alert_id=1,
    )
    session.add(event)
    await session.flush()

    r1 = DeepAnalysis(
        webhook_event_id=event.id,
        engine="local",
        user_question="",
        analysis_result={"root_cause": "x"},
        status="completed",
        created_at=datetime(2026, 1, 1, 0, 0, 0),
    )
    r2 = DeepAnalysis(
        webhook_event_id=999,
        engine="local",
        user_question="",
        analysis_result={"root_cause": "y"},
        status="completed",
        created_at=datetime(2026, 1, 1, 0, 0, 1),
    )
    session.add_all([r1, r2])
    await session.commit()

    monkeypatch.setattr("api.deep_analysis.MAX_PAGE", 2)

    resp = await list_all_deep_analyses(page=1, per_page=20, cursor=None, status="", engine="", session=session)
    assert resp["success"] is True
    items = resp["data"]["items"]
    assert len(items) == 2

    by_id = {i["webhook_event_id"]: i for i in items}
    assert by_id[event.id]["source"] == "prometheus"
    assert by_id[event.id]["is_duplicate"] is True

    assert by_id[999]["source"] is None
    assert by_id[999]["is_duplicate"] is False


async def test_get_deep_analyses_returns_serializable_dicts(session):
    from api.deep_analysis import get_deep_analyses
    from models import DeepAnalysis, WebhookEvent

    event = WebhookEvent(
        source="prometheus",
        client_ip="127.0.0.1",
        timestamp=datetime(2026, 1, 1, 0, 0, 0),
        importance="high",
        processing_status="completed",
        is_duplicate=False,
        duplicate_count=1,
    )
    session.add(event)
    await session.flush()

    record = DeepAnalysis(
        webhook_event_id=event.id,
        engine="openclaw",
        user_question="",
        analysis_result={"root_cause": "x"},
        status="completed",
        created_at=datetime(2026, 1, 1, 0, 1, 0),
    )
    session.add(record)
    await session.commit()

    resp = await get_deep_analyses(webhook_id=event.id, session=session)
    assert resp["success"] is True
    assert isinstance(resp["data"][0], dict)
    assert resp["data"][0]["webhook_event_id"] == event.id
    assert resp["data"][0]["analysis_result"] == {"root_cause": "x"}


async def test_retry_deep_analysis_schedules_background_poll(session, monkeypatch):
    from api import deep_analysis
    from models import DeepAnalysis, WebhookEvent
    from services.webhooks.types import DeepAnalysisStatus

    event = WebhookEvent(
        source="volcengine",
        client_ip="127.0.0.1",
        timestamp=datetime(2026, 1, 1, 0, 0, 0),
        importance="high",
        processing_status="completed",
        is_duplicate=False,
        duplicate_count=1,
    )
    session.add(event)
    await session.flush()

    old_created_at = utcnow() - timedelta(hours=2)
    record = DeepAnalysis(
        webhook_event_id=event.id,
        engine="openclaw",
        user_question="",
        analysis_result={"root_cause": "old timeout"},
        status=DeepAnalysisStatus.TIMEOUT,
        created_at=old_created_at,
        openclaw_run_id="run-1",
        openclaw_session_key="session-1",
        poll_attempts=4,
        last_polled_at=old_created_at,
        next_poll_at=old_created_at,
    )
    session.add(record)
    await session.commit()

    scheduled: list[tuple[int, int]] = []
    cleared: list[int] = []

    async def fake_schedule(analysis_id: int, delay_seconds: int) -> None:
        scheduled.append((analysis_id, delay_seconds))

    async def fake_clear(record_id: int) -> None:
        cleared.append(record_id)

    async def fail_if_called(*_: object, **__: object) -> tuple[dict[str, object], str]:
        raise AssertionError("retry with an existing session_key should not block on remote analysis")

    monkeypatch.setattr("services.operations.taskiq_retry_scheduler.schedule_openclaw_poll_best_effort", fake_schedule)
    monkeypatch.setattr("services.analysis.openclaw.clear_openclaw_poll_state", fake_clear)
    monkeypatch.setattr(deep_analysis, "_run_openclaw_deep_analysis", fail_if_called)

    started = utcnow()
    resp = await deep_analysis.retry_deep_analysis(record.id, session=session)

    assert resp["success"] is True
    assert scheduled == [(record.id, 0)]
    assert cleared == [record.id]

    await session.refresh(record)
    assert record.status == DeepAnalysisStatus.PENDING
    assert isinstance(record.analysis_result, dict)
    retry_started_at = record.analysis_result[deep_analysis.MANUAL_RETRY_STARTED_AT_KEY]
    assert datetime.fromisoformat(str(retry_started_at)) >= started
    assert record.duration_seconds == 0
    assert record.poll_attempts == 0
    assert record.last_polled_at is None
    assert record.created_at == old_created_at
    assert record.next_poll_at is not None and record.next_poll_at >= started


def test_webhook_analysis_result_to_dict_dumps_enum_to_string():
    from schemas.analysis import Importance, WebhookAnalysisResult

    r = WebhookAnalysisResult(
        source="prometheus",
        event_type="PrometheusAlert",
        importance=Importance.HIGH,
        summary="x",
        actions=[],
        risks=[],
        monitoring_suggestions=[],
    )
    d = r.to_dict()
    assert d["importance"] == "high"

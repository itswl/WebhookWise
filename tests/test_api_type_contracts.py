from datetime import datetime

import pytest
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles

pytest.importorskip("fastapi")


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


@pytest.fixture()
async def session(monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    # Import models to register them with Base.metadata
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
    from api.webhook import list_webhooks_cursor
    from models import WebhookEvent

    t0 = datetime(2026, 1, 1, 0, 0, 0)
    t1 = datetime(2026, 1, 1, 0, 1, 0)

    e1 = WebhookEvent(
        source="test",
        client_ip="127.0.0.1",
        timestamp=t0,
        importance="high",
        processing_status="completed",
        is_duplicate=0,
        duplicate_of=None,
        duplicate_count=1,
        beyond_window=0,
        prev_alert_id=None,
    )
    e2 = WebhookEvent(
        source="test",
        client_ip="127.0.0.2",
        timestamp=t1,
        importance="high",
        processing_status="completed",
        is_duplicate=1,
        duplicate_of=1,
        duplicate_count=2,
        beyond_window=0,
        prev_alert_id=1,
    )
    session.add_all([e1, e2])
    await session.commit()

    r = await list_webhooks_cursor(limit=200, fields="summary", session=session)
    assert r["success"] is True
    assert "pagination" in r
    assert "has_more" in r["pagination"]
    assert "next_cursor" in r["pagination"]
    assert isinstance(r["data"], list)
    assert len(r["data"]) == 2

    newest = r["data"][0]
    assert newest["id"] == 2
    assert newest["prev_alert_id"] == 1
    assert newest["prev_alert_timestamp"] == t0.isoformat()

    oldest = r["data"][1]
    assert oldest["id"] == 1
    assert oldest["prev_alert_id"] is None
    assert oldest["prev_alert_timestamp"] is None


async def test_deep_analyses_list_fields(session, monkeypatch):
    from api.deep_analysis import list_all_deep_analyses
    from models import DeepAnalysis, WebhookEvent

    event = WebhookEvent(
        source="prometheus",
        client_ip="127.0.0.1",
        timestamp=datetime(2026, 1, 1, 0, 0, 0),
        importance="medium",
        processing_status="completed",
        is_duplicate=1,
        duplicate_of=1,
        duplicate_count=2,
        beyond_window=1,
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

    resp = await list_all_deep_analyses(
        page=1, per_page=20, cursor=None, status_filter="", engine_filter="", session=session
    )
    assert resp["success"] is True
    items = resp["data"]["items"]
    assert len(items) == 2

    by_id = {i["webhook_event_id"]: i for i in items}
    assert by_id[event.id]["source"] == "prometheus"
    assert by_id[event.id]["is_duplicate"] is True
    assert by_id[event.id]["beyond_window"] is True

    assert by_id[999]["source"] is None
    assert by_id[999]["is_duplicate"] is False
    assert by_id[999]["beyond_window"] is False

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime

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


@pytest.mark.asyncio
async def test_compute_noise_persists_suppressed_record(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from models import SuppressedRecord
    from services.analysis.analysis_policies import NoiseScoringConfig
    from services.webhooks.noise_stage import compute_noise
    from services.webhooks.policies import NoiseReductionPolicy

    class Decision:
        relation = "derived"
        root_cause_event_id = 101
        confidence = 0.91
        suppress_forward = True
        reason = "derived alert"
        related_alert_count = 2
        related_alert_ids = [101, 102]

    monkeypatch.setattr("services.webhooks.noise_stage.analyze_noise_reduction", lambda *a, **k: Decision())

    async def _no_recent(*_: object, **__: object) -> list[object]:
        return []

    monkeypatch.setattr("services.webhooks.noise_stage.list_recent_alert_contexts", _no_recent)

    policy = NoiseReductionPolicy(
        enabled=True,
        window_minutes=60,
        root_cause_min_confidence=0.5,
        suppress_derived_forward=True,
        scoring_config=NoiseScoringConfig(
            source_weight=1.0,
            resource_weight=1.0,
            semantic_weight=1.0,
            severity_weight=1.0,
            time_weight=1.0,
            severity_downgrade_score=0.1,
            related_min_confidence=0.3,
        ),
    )

    noise = await compute_noise(
        "hash-1",
        "prometheus",
        {"labels": {"severity": "warning"}},
        {"importance": "high", "summary": "x", "event_type": "t"},
        policy=policy,
    )
    assert noise.suppress_forward is True

    async with session_factory() as session:
        record = (await session.execute(select(SuppressedRecord))).scalars().first()
        assert record is not None
        assert record.alert_hash == "hash-1"
        assert record.source == "prometheus"
        assert record.relation == "derived"
        assert record.root_cause_event_id == 101
        assert record.related_alert_ids == [101, 102]
        assert record.reason == "derived alert"
        assert record.created_at is not None


@pytest.mark.asyncio
async def test_suppressed_service_lists_records(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from models import SuppressedRecord
    from services.webhooks.repository import list_suppressed_records

    now = datetime.now()
    async with session_factory.begin() as session:
        session.add(
            SuppressedRecord(
                alert_hash="hash-2",
                source="grafana",
                relation="derived",
                root_cause_event_id=None,
                reason="x",
                related_alert_ids=[1],
                confidence=0.5,
                created_at=now,
            )
        )

    async with session_factory() as session:
        items = await list_suppressed_records(session, since_minutes=60, limit=10)
    assert len(items) == 1
    assert items[0]["alert_hash"] == "hash-2"

"""Silence API handler tests (direct calls, in-memory sqlite)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool


def _body(response: Any) -> dict[str, Any]:
    return json.loads(response.body)


@pytest.fixture()
async def session() -> AsyncIterator[AsyncSession]:
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
    # Plain session (not factory.begin()): the endpoint handlers issue their own
    # commit(), which would close a begin()-managed transaction mid-test.
    async with factory() as sess:
        yield sess
    await engine.dispose()


# ── Silence routes ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_list_lift_silence_flow(session: AsyncSession) -> None:
    from api.v1 import silences as api
    from schemas.silences import SilenceCreateRequest

    created = await api.create_silence_endpoint(
        SilenceCreateRequest(match_source="prometheus", comment="maintenance"), session=session
    )
    assert created["success"] is True
    silence_id = created["data"]["id"]
    assert created["data"]["active"] is True

    listed = await api.list_silences_endpoint(active_only=True, session=session)
    assert len(listed["data"]) == 1

    lifted = await api.lift_silence_endpoint(silence_id, session=session)
    assert lifted["data"]["active"] is False

    # active_only now excludes it, but the full list still shows it.
    active = await api.list_silences_endpoint(active_only=True, session=session)
    assert active["data"] == []
    full = await api.list_silences_endpoint(active_only=False, session=session)
    assert len(full["data"]) == 1


@pytest.mark.asyncio
async def test_list_silences_annotates_suppression_counts(session: AsyncSession) -> None:
    from api.v1 import silences as api
    from models import DecisionTrace
    from schemas.silences import SilenceCreateRequest

    # Two silences: one that has suppressed alerts, one "zombie" that hasn't.
    busy = await api.create_silence_endpoint(
        SilenceCreateRequest(match_source="volcengine", comment="busy"), session=session
    )
    zombie = await api.create_silence_endpoint(
        SilenceCreateRequest(match_source="aliyun", comment="zombie"), session=session
    )
    busy_id = busy["data"]["id"]

    # Seed two silenced decision traces attributed to the busy rule.
    session.add_all(
        [
            DecisionTrace(webhook_event_id=1, outcome="skipped", skip_code="silenced", silence_id=busy_id),
            DecisionTrace(webhook_event_id=2, outcome="skipped", skip_code="silenced", silence_id=busy_id),
        ]
    )
    await session.commit()

    listed = await api.list_silences_endpoint(session=session)
    by_id = {s["id"]: s for s in listed["data"]}
    assert by_id[busy_id]["suppressed_count"] == 2
    assert by_id[busy_id]["last_suppressed_at"] is not None
    # The zombie rule reports zero, with no last-suppressed timestamp.
    assert by_id[zombie["data"]["id"]]["suppressed_count"] == 0
    assert by_id[zombie["data"]["id"]]["last_suppressed_at"] is None


@pytest.mark.asyncio
async def test_create_silence_normalizes_aware_expiry(session: AsyncSession) -> None:
    from api.v1 import silences as api
    from schemas.silences import SilenceCreateRequest

    aware = datetime.now(tz=UTC) + timedelta(hours=2)
    created = await api.create_silence_endpoint(
        SilenceCreateRequest(match_source="prometheus", expires_at=aware), session=session
    )
    # Stored naive-UTC; serialized back with a trailing Z.
    assert created["data"]["expires_at"].endswith("Z")


@pytest.mark.asyncio
async def test_create_silence_requires_a_criterion() -> None:
    from schemas.silences import SilenceCreateRequest

    with pytest.raises(ValueError, match="At least one match criterion"):
        SilenceCreateRequest(comment="no criteria")


@pytest.mark.asyncio
async def test_lift_missing_silence_returns_404(session: AsyncSession) -> None:
    from api.v1 import silences as api

    resp = await api.lift_silence_endpoint(9999, session=session)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_silence(session: AsyncSession) -> None:
    from api.v1 import silences as api
    from schemas.silences import SilenceCreateRequest

    created = await api.create_silence_endpoint(
        SilenceCreateRequest(match_source="prometheus"), session=session
    )
    resp = await api.delete_silence_endpoint(created["data"]["id"], session=session)
    assert resp["success"] is True
    missing = await api.delete_silence_endpoint(created["data"]["id"], session=session)
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_update_silence_make_permanent(session: AsyncSession) -> None:
    from api.v1 import silences as api
    from schemas.silences import SilenceCreateRequest, SilenceUpdateRequest

    created = await api.create_silence_endpoint(
        SilenceCreateRequest(match_source="prometheus", expires_at=datetime.now(tz=UTC) + timedelta(hours=1)),
        session=session,
    )
    updated = await api.update_silence_endpoint(
        created["data"]["id"], SilenceUpdateRequest(expires_at=None), session=session
    )
    assert updated["data"]["expires_at"] is None
    assert updated["data"]["active"] is True

"""Silence + acknowledgement API handler tests (direct calls, in-memory sqlite)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from models import WebhookEvent


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


# ── Acknowledgement routes ───────────────────────────────────────────────────


async def _make_event(session: AsyncSession) -> WebhookEvent:
    event = WebhookEvent(source="prometheus", is_duplicate=False, duplicate_count=1)
    session.add(event)
    await session.flush()
    return event


@pytest.mark.asyncio
async def test_ack_and_unack_endpoints(session: AsyncSession) -> None:
    from api.v1 import webhook as api
    from schemas.webhook import WebhookAckRequest

    event = await _make_event(session)

    acked = await api.acknowledge_webhook_endpoint(
        event.id, WebhookAckRequest(acknowledged_by="alice"), session=session
    )
    assert acked["success"] is True
    assert acked["data"]["acknowledged"] is True
    assert acked["data"]["acknowledged_by"] == "alice"
    assert acked["data"]["acknowledged_at"].endswith("Z")

    unacked = await api.unacknowledge_webhook_endpoint(event.id, session=session)
    assert unacked["data"]["acknowledged"] is False


@pytest.mark.asyncio
async def test_ack_without_body(session: AsyncSession) -> None:
    from api.v1 import webhook as api

    event = await _make_event(session)
    acked = await api.acknowledge_webhook_endpoint(event.id, None, session=session)
    assert acked["data"]["acknowledged"] is True
    assert acked["data"]["acknowledged_by"] is None


@pytest.mark.asyncio
async def test_ack_missing_event_returns_404(session: AsyncSession) -> None:
    from api.v1 import webhook as api

    resp = await api.acknowledge_webhook_endpoint(9999, None, session=session)
    assert resp.status_code == 404

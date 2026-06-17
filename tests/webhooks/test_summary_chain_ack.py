"""Summary list reports ack state from the dedup-chain head.

Acknowledgement is stored on the chain head (the original event). A duplicate
occurrence must reflect its head's ack state in the list, otherwise acking a
duplicate card would leave it visually unchanged.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from core.datetime_utils import utcnow
from models import WebhookEvent
from services.webhooks.query_service import list_webhook_summaries


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
    async with factory() as sess:
        yield sess
    await engine.dispose()


def _by_id(items: list[dict], item_id: int) -> dict:
    return next(i for i in items if i["id"] == item_id)


@pytest.mark.asyncio
async def test_duplicate_reflects_head_ack(session: AsyncSession) -> None:
    head = WebhookEvent(source="prometheus", is_duplicate=False, duplicate_count=2, acknowledged_at=utcnow(), acknowledged_by="alice")
    session.add(head)
    await session.flush()
    dup = WebhookEvent(source="prometheus", is_duplicate=True, duplicate_of=head.id, duplicate_count=1)
    session.add(dup)
    await session.commit()

    items, _, _ = await list_webhook_summaries(session, page_size=50)
    # Both the head AND the duplicate report acknowledged=true (chain-level).
    assert _by_id(items, head.id)["acknowledged"] is True
    assert _by_id(items, dup.id)["acknowledged"] is True
    assert _by_id(items, dup.id)["acknowledged_by"] == "alice"


@pytest.mark.asyncio
async def test_unacked_chain_reports_false(session: AsyncSession) -> None:
    head = WebhookEvent(source="prometheus", is_duplicate=False, duplicate_count=2)
    session.add(head)
    await session.flush()
    dup = WebhookEvent(source="prometheus", is_duplicate=True, duplicate_of=head.id, duplicate_count=1)
    session.add(dup)
    await session.commit()

    items, _, _ = await list_webhook_summaries(session, page_size=50)
    assert _by_id(items, head.id)["acknowledged"] is False
    assert _by_id(items, dup.id)["acknowledged"] is False
    assert _by_id(items, dup.id)["acknowledged_at"] is None


@pytest.mark.asyncio
async def test_head_self_ack_still_reported(session: AsyncSession) -> None:
    # A non-duplicate (head) event with no duplicate_of reflects its own ack via coalesce.
    head = WebhookEvent(source="grafana", is_duplicate=False, duplicate_count=1, acknowledged_at=utcnow())
    session.add(head)
    await session.commit()

    items, _, _ = await list_webhook_summaries(session, page_size=50)
    assert _by_id(items, head.id)["acknowledged"] is True


@pytest.mark.asyncio
async def test_acknowledged_filter_matches_chain_head(session: AsyncSession) -> None:
    # Acked chain: head + duplicate. Unacked standalone event.
    acked_head = WebhookEvent(source="prometheus", is_duplicate=False, duplicate_count=2, acknowledged_at=utcnow())
    session.add(acked_head)
    await session.flush()
    acked_dup = WebhookEvent(source="prometheus", is_duplicate=True, duplicate_of=acked_head.id, duplicate_count=1)
    plain = WebhookEvent(source="grafana", is_duplicate=False, duplicate_count=1)
    session.add_all([acked_dup, plain])
    await session.commit()

    acked, _, _ = await list_webhook_summaries(session, acknowledged=True, page_size=50)
    acked_ids = {i["id"] for i in acked}
    # Both the head AND its duplicate are returned (chain-level); the plain one is not.
    assert acked_head.id in acked_ids
    assert acked_dup.id in acked_ids
    assert plain.id not in acked_ids

    unacked, _, _ = await list_webhook_summaries(session, acknowledged=False, page_size=50)
    unacked_ids = {i["id"] for i in unacked}
    assert plain.id in unacked_ids
    assert acked_head.id not in unacked_ids
    assert acked_dup.id not in unacked_ids

    # No filter → everything.
    all_items, _, _ = await list_webhook_summaries(session, page_size=50)
    assert len(all_items) == 3

"""Forward-rule ROI enrichment on the list endpoint (direct calls, sqlite)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool


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


@pytest.mark.asyncio
async def test_list_forward_rules_annotates_hit_counts(session: AsyncSession) -> None:
    from api.v1 import forwarding as api
    from models import DecisionTrace, ForwardRule

    # Two rules: one that has matched alerts, one enabled "zombie" that hasn't.
    busy = ForwardRule(name="busy-rule", target_type="feishu", target_url="https://example.com/hook/x", enabled=True)
    zombie = ForwardRule(name="zombie-rule", target_type="feishu", target_url="https://example.com/hook/y", enabled=True)
    session.add_all([busy, zombie])
    # Two forwarded traces that matched busy-rule.
    session.add_all(
        [
            DecisionTrace(webhook_event_id=1, outcome="forwarded", skip_code="none", matched_rules=["busy-rule"]),
            DecisionTrace(webhook_event_id=2, outcome="forwarded", skip_code="none", matched_rules=["busy-rule"]),
        ]
    )
    await session.commit()

    result = await api.get_forward_rules_endpoint(session=session)
    by_name = {r["name"]: r for r in result["data"]}
    assert by_name["busy-rule"]["hit_count"] == 2
    assert by_name["busy-rule"]["last_matched_at"] is not None
    # The zombie rule reports zero, no last-matched timestamp.
    assert by_name["zombie-rule"]["hit_count"] == 0
    assert by_name["zombie-rule"]["last_matched_at"] is None
    # Masked list must not leak the raw target URL secret.
    assert "example.com/hook/x" not in str(by_name["busy-rule"]["target_url"])

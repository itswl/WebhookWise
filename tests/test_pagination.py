"""
测试分页查询功能
"""

from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.ext.compiler import compiles

from db.session import Base
from models import WebhookEvent
from services.webhooks.query_service import list_webhook_summaries


# SQLite 不原生支持 JSONB，DDL 编译时降级为 JSON
@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_: Any, compiler: Any, **kw: Any) -> str:
    return "JSON"


@pytest.fixture()
async def mock_session_scope() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    Session = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    # 插入一些测试数据
    async with Session() as session:
        for _i in range(1, 16):
            event = WebhookEvent(
                source="test",
                importance="high",
                is_duplicate=False,
                duplicate_count=1,
                beyond_window=False,
            )
            session.add(event)
        await session.commit()

    yield Session
    await engine.dispose()


@pytest.mark.asyncio
async def test_list_webhook_summaries_pagination(
    mock_session_scope: async_sessionmaker[AsyncSession],
) -> None:
    # 测试第一页
    async with mock_session_scope() as session:
        webhooks, has_more, next_cursor = await list_webhook_summaries(session, cursor_id=None, page_size=5)
    assert len(webhooks) == 5
    assert has_more is True
    assert next_cursor == 11

    # 测试第二页
    async with mock_session_scope() as session:
        webhooks, has_more, next_cursor = await list_webhook_summaries(session, cursor_id=11, page_size=5)
    assert len(webhooks) == 5
    assert has_more is True
    assert next_cursor == 6

    # 测试第三页
    async with mock_session_scope() as session:
        webhooks, has_more, next_cursor = await list_webhook_summaries(session, cursor_id=6, page_size=5)
    assert len(webhooks) == 5
    assert has_more is False
    assert next_cursor is None  # 5, 4, 3, 2, 1 (没有更多了)

    # 验证最后一页
    async with mock_session_scope() as session:
        webhooks, has_more, next_cursor = await list_webhook_summaries(session, cursor_id=1, page_size=5)
    assert len(webhooks) == 0
    assert has_more is False
    assert next_cursor is None


@pytest.mark.asyncio
async def test_list_webhook_summaries_page_offset_without_cursor(
    mock_session_scope: async_sessionmaker[AsyncSession],
) -> None:
    async with mock_session_scope() as session:
        webhooks, has_more, next_cursor = await list_webhook_summaries(session, page=2, page_size=5)

    assert [item["id"] for item in webhooks] == [10, 9, 8, 7, 6]
    assert has_more is True
    assert next_cursor == 6

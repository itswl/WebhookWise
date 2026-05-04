"""
测试分页查询功能
"""

import pytest
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles

from db.session import Base
from models import WebhookEvent
from services.webhook_orchestrator import get_all_webhooks


# SQLite 不原生支持 JSONB，DDL 编译时降级为 JSON
@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


@pytest.fixture()
async def mock_session_scope(monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    Session = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    def _mock_session_scope():
        return Session()

    # 替换数据库连接
    monkeypatch.setattr("services.webhook_orchestrator.session_scope", _mock_session_scope)

    # 插入一些测试数据
    async with Session() as session:
        for _i in range(1, 16):
            event = WebhookEvent(
                source="test",
                importance="high",
                is_duplicate=0,
                duplicate_count=1,
                beyond_window=0,
            )
            session.add(event)
        await session.commit()

    yield Session
    await engine.dispose()


@pytest.mark.asyncio
async def test_get_all_webhooks_pagination(mock_session_scope, monkeypatch):
    # 注入 mock session
    from db import session as db_session
    def _mock_session_factory():
        return mock_session_scope()

    monkeypatch.setattr(db_session, "_session_factory", _mock_session_factory)

    # 测试第一页
    webhooks, total, next_cursor = await get_all_webhooks(cursor_id=None, page_size=5)
    assert len(webhooks) == 5
    assert next_cursor == 11  # 15, 14, 13, 12, 11 -> 下一个应该是 10?
    # 注意：我们的 list_webhook_summaries 使用的是 WebhookEvent.id < cursor_id
    # 15, 14, 13, 12, 11 (5条) -> rows[-1].id = 11

    # 测试第二页
    webhooks, total, next_cursor = await get_all_webhooks(cursor_id=11, page_size=5)
    assert len(webhooks) == 5
    assert next_cursor == 6

    # 测试第三页
    webhooks, total, next_cursor = await get_all_webhooks(cursor_id=6, page_size=5)
    assert len(webhooks) == 5
    assert next_cursor is None  # 5, 4, 3, 2, 1 (没有更多了)

    # 验证最后一页
    webhooks, total, next_cursor = await get_all_webhooks(cursor_id=1, page_size=5)
    assert len(webhooks) == 0
    assert next_cursor is None

"""
测试分页查询功能
"""

import contextlib
from datetime import datetime

import pytest
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles

from crud.webhook import get_all_webhooks
from db.session import Base
from models import WebhookEvent


# SQLite 不原生支持 JSONB，DDL 编译时降级为 JSON
@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


@pytest.fixture(autouse=True)
async def setup_test_db(monkeypatch):
    """
    配置独立的内存 SQLite 数据库供分页测试使用。
    """
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    from sqlalchemy.ext.asyncio import async_sessionmaker

    Session = async_sessionmaker(bind=engine, class_=AsyncSession)

    @contextlib.asynccontextmanager
    async def mock_session_scope():
        session = Session()
        try:
            yield session
            await session.commit()
        except:
            await session.rollback()
            raise
        finally:
            await session.close()

    # 替换数据库连接
    monkeypatch.setattr("crud.webhook.session_scope", mock_session_scope)

    # 插入一些测试数据
    async with mock_session_scope() as session:
        for i in range(1, 16):
            event = WebhookEvent(
                source="test",
                importance="high",
                forward_status="success",
                client_ip="127.0.0.1",
                is_duplicate=0,
                duplicate_count=0,
                beyond_window=0,
                alert_hash=f"hash{i}",
                timestamp=datetime.now(),
                parsed_data={"title": f"Test {i}"},
            )
            session.add(event)
        await session.commit()


async def test_pagination():
    """测试 Keyset 游标分页"""
    # 首次请求（无 cursor）：返回最新的 page_size+1 条判断 has_more，取前 page_size 条
    webhooks, total, next_cursor = await get_all_webhooks(page=1, page_size=5)
    assert len(webhooks) == 5
    assert webhooks[0]["id"] == 15
    assert webhooks[-1]["id"] == 11
    # total 在 keyset 模式下不再提供精确值
    assert total == -1
    # 有更多数据时返回 next_cursor
    assert next_cursor == 11

    # 使用 cursor_id 翻到第二页
    webhooks, total, next_cursor = await get_all_webhooks(cursor_id=11, page_size=5)
    assert len(webhooks) == 5
    assert webhooks[0]["id"] == 10
    assert webhooks[-1]["id"] == 6
    assert next_cursor == 6

    # 使用 cursor_id 翻到第三页（最后一页）
    webhooks, total, next_cursor = await get_all_webhooks(cursor_id=6, page_size=5)
    assert len(webhooks) == 5
    assert webhooks[0]["id"] == 5
    assert webhooks[-1]["id"] == 1
    # 没有更多数据时 next_cursor 为 None
    assert next_cursor is None

    # 游标指向不存在的范围
    webhooks, total, next_cursor = await get_all_webhooks(cursor_id=1, page_size=5)
    assert len(webhooks) == 0
    assert next_cursor is None

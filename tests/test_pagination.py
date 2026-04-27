"""
测试分页查询功能
"""
import contextlib
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from crud.webhook import get_all_webhooks
from db.session import Base
from models import WebhookEvent


@pytest.fixture(autouse=True)
def setup_test_db(monkeypatch):
    """
    配置独立的内存 SQLite 数据库供分页测试使用。
    """
    engine = create_engine('sqlite:///:memory:', echo=False)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    
    @contextlib.contextmanager
    def mock_session_scope():
        session = Session()
        try:
            yield session
            session.commit()
        except:
            session.rollback()
            raise
        finally:
            session.close()

    # 替换数据库连接
    monkeypatch.setattr('crud.webhook.session_scope', mock_session_scope)

    # 插入一些测试数据
    with mock_session_scope() as session:
        for i in range(1, 16):
            event = WebhookEvent(
                source='test',
                importance='high',
                forward_status='success',
                client_ip='127.0.0.1',
                is_duplicate=0,
                duplicate_count=0,
                beyond_window=0,
                alert_hash=f"hash{i}",
                timestamp=datetime.now(),
                parsed_data={"title": f"Test {i}"}
            )
            session.add(event)
        session.commit()


def test_pagination():
    """测试分页查询"""
    # 测试第一页
    webhooks, total, next_cursor = get_all_webhooks(page=1, page_size=5)
    assert len(webhooks) == 5
    assert total == 15
    assert webhooks[0]['id'] == 15
    assert webhooks[-1]['id'] == 11
    
    # 测试第二页
    webhooks, total, next_cursor = get_all_webhooks(page=2, page_size=5)
    assert len(webhooks) == 5
    assert webhooks[0]['id'] == 10
    assert webhooks[-1]['id'] == 6
    
    # 测试第三页
    webhooks, total, next_cursor = get_all_webhooks(page=3, page_size=5)
    assert len(webhooks) == 5
    assert webhooks[0]['id'] == 5
    assert webhooks[-1]['id'] == 1
    
    # 测试大页码
    webhooks, total, next_cursor = get_all_webhooks(page=100, page_size=5)
    assert len(webhooks) == 0
    
    # 测试游标分页
    webhooks, total, next_cursor = get_all_webhooks(cursor_id=10, page_size=5)
    assert len(webhooks) == 5
    assert all(w['id'] < 10 for w in webhooks)

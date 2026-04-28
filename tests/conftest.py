"""
tests/conftest.py
=================
pytest 全局 fixtures：外部服务 mock、测试数据库 session。
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_ps = str(_ROOT)
if _ps not in sys.path:
    sys.path.insert(0, _ps)


# ── 外部服务 Mock ─────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def mock_requests():
    """所有测试默认 mock requests，避免发起真实 HTTP 请求。"""
    with patch("requests.post") as mock_post, patch("requests.get") as mock_get:
        # 默认返回成功响应
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b"{}"
        mock_response.text = "{}"
        mock_response.json.return_value = {}
        mock_response.raise_for_status = MagicMock()

        mock_post.return_value = mock_response
        mock_get.return_value = mock_response

        yield {"post": mock_post, "get": mock_get}


# ── 数据库 Session Fixture ────────────────────────────────────────────────────


@pytest.fixture
def test_db_session():
    """
    提供一个独立内存 SQLite session 用于单元测试。
    使用前需 monkeypatch model 的 engine：
        monkeypatch.setattr('core.models.create_engine', lambda *a, **k: create_engine('sqlite:///:memory:'))
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine("sqlite:///:memory:", echo=False)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


# ── Config Override Fixture ───────────────────────────────────────────────────


@pytest.fixture
def temp_config():
    """临时覆盖 Config 属性，测试结束后自动恢复。"""
    from core.config import Config

    original = {k: getattr(Config, k) for k in dir(Config) if not k.startswith("_")}
    yield Config
    for k, v in original.items():
        setattr(Config, k, v)

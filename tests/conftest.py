"""
tests/conftest.py
=================
pytest 全局 fixtures：外部服务 mock、测试数据库 session。
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b"{}"
        mock_response.text = "{}"
        mock_response.json.return_value = {}
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response
        mock_get.return_value = mock_response
        yield {"post": mock_post, "get": mock_get}


@pytest.fixture(autouse=True)
def mock_httpx():
    """Mock httpx async client，阻止所有真实 HTTP 请求。"""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = "{}"
    mock_resp.json.return_value = {}
    mock_resp.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_resp
    mock_client.get.return_value = mock_resp

    with patch("core.http_client.get_http_client", return_value=mock_client):
        yield mock_client


@pytest.fixture(autouse=True)
def mock_redis():
    """Mock Redis 客户端，避免连接真实 Redis。"""
    mock = AsyncMock()
    mock.get.return_value = None
    mock.set.return_value = True
    mock.setex.return_value = True
    mock.incr.return_value = 1
    mock.expire.return_value = True
    mock.publish.return_value = 0
    pipeline_mock = AsyncMock()
    pipeline_mock.execute.return_value = [1, True]
    pipeline_mock.incr = AsyncMock()
    pipeline_mock.expire = AsyncMock()
    pipeline_mock.setex = AsyncMock()
    mock.pipeline.return_value = pipeline_mock
    with patch("core.redis_client.get_redis", return_value=mock):
        yield mock


# ── Config Override Fixture ───────────────────────────────────────────────────


@pytest.fixture
def temp_config():
    """临时覆盖 Config 属性，测试结束后自动恢复。"""
    from core.config import Config

    snapshots: dict[str, dict[str, object]] = {}
    for sub_name in Config._SUB_NAMES:
        sub = getattr(Config, sub_name)
        snapshots[sub_name] = {k: getattr(sub, k) for k in sub.model_fields}
    yield Config
    for sub_name, fields in snapshots.items():
        sub = getattr(Config, sub_name)
        for k, v in fields.items():
            setattr(sub, k, v)

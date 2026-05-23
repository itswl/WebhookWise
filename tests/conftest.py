"""
tests/conftest.py
=================
pytest 全局 fixtures：外部服务 mock、测试数据库 session。
"""

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_ps = str(_ROOT)
if _ps not in sys.path:
    sys.path.insert(0, _ps)


@pytest.fixture(scope="session", autouse=True)
def disable_otel_for_tests():
    """Keep local pytest runs from starting background OTEL exporters from .env."""
    old_enabled = os.environ.get("OTEL_ENABLED")
    os.environ["OTEL_ENABLED"] = "false"
    yield
    if old_enabled is None:
        os.environ.pop("OTEL_ENABLED", None)
    else:
        os.environ["OTEL_ENABLED"] = old_enabled


@pytest.fixture(scope="session", autouse=True)
def initialize_adapter_registry():
    """测试进程启动时注册 adapter，避免请求路径做动态注册。"""
    from adapters.ecosystem_adapters import initialize_adapters

    initialize_adapters()


@pytest.fixture(autouse=True)
def reset_default_app_context():
    """Keep AppContext-owned resources isolated between tests."""
    from core.app_context import AppContext, set_default_app_context
    from core.config import UnifiedConfigManager, get_settings
    from core.redis_health import reset_redis_health

    settings = get_settings().model_copy(deep=True)
    context = AppContext(config=UnifiedConfigManager(settings))
    reset_redis_health()
    set_default_app_context(context)
    app_module = sys.modules.get("core.app")
    if app_module is not None:
        app_module.app.state.app_context = context
    yield
    set_default_app_context(None)
    reset_redis_health()


# ── 外部服务 Mock ─────────────────────────────────────────────────────────────


def _has_marker(request: pytest.FixtureRequest, *names: str) -> bool:
    return any(request.node.get_closest_marker(name) is not None for name in names)


@pytest.fixture(autouse=True)
def mock_requests(request: pytest.FixtureRequest):
    """所有测试默认 mock requests，避免发起真实 HTTP 请求。"""
    if _has_marker(request, "real_requests", "real_network", "real_services"):
        yield None
        return
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
def mock_httpx(request: pytest.FixtureRequest):
    """Mock httpx async client，阻止所有真实 HTTP 请求。"""
    if _has_marker(request, "real_httpx", "real_network", "real_services"):
        yield None
        return
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
def mock_redis(request: pytest.FixtureRequest):
    """Mock Redis 客户端，避免连接真实 Redis。"""
    if _has_marker(request, "real_redis", "real_services"):
        yield None
        return

    async def eval_script(script: str, numkeys: int, *args: object) -> object:
        if numkeys == 2 and "open_until" in script:
            return "closed"
        return 1

    mock = AsyncMock()
    mock.get.return_value = None
    mock.set.return_value = True
    mock.setex.return_value = True
    mock.incr.return_value = 1
    mock.expire.return_value = True
    mock.eval.side_effect = eval_script
    mock.publish.return_value = 0
    pipeline_mock = AsyncMock()
    pipeline_mock.execute.return_value = [1, True]
    pipeline_mock.incr = AsyncMock()
    pipeline_mock.expire = AsyncMock()
    pipeline_mock.setex = AsyncMock()
    mock.pipeline.return_value = pipeline_mock
    with patch("core.redis_client.get_redis", return_value=mock):
        yield mock


# ── Configuration Override Fixture ────────────────────────────────────────────


@pytest.fixture
def temp_config():
    """Return this test's isolated AppContext configuration manager."""
    from core.app_context import get_default_config

    yield get_default_config()

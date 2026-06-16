"""
tests/conftest.py
=================
Global pytest fixtures: external-service mocks and the test database session.
"""

import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles

_ROOT = Path(__file__).resolve().parents[1]
_ps = str(_ROOT)
if _ps not in sys.path:
    sys.path.insert(0, _ps)

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://webhook_user:test-password@localhost:5432/webhooks_test")


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_: Any, compiler: Any, **kw: Any) -> str:
    """Let SQLite-backed unit tests create models that use PostgreSQL JSONB."""
    return "JSON"


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
    """Register adapters when the test process starts so the request path doesn't register them dynamically."""
    from adapters.ecosystem_adapters import initialize_adapters

    initialize_adapters()


@pytest.fixture(autouse=True)
def reset_default_app_context():
    """Keep AppContext-owned resources isolated between tests."""
    from core.app_context import AppContext, set_default_app_context
    from core.config import get_settings
    from core.redis_health import reset_redis_health
    from services.forwarding.rules import invalidate_forward_rules_cache

    settings = get_settings().model_copy(deep=True)
    context = AppContext(config=settings)
    reset_redis_health()
    invalidate_forward_rules_cache()
    set_default_app_context(context)
    app_module = sys.modules.get("api.app")
    if app_module is not None:
        app_module.app.state.app_context = context
    yield
    set_default_app_context(None)
    reset_redis_health()
    invalidate_forward_rules_cache()


# ── External Service Mocks ────────────────────────────────────────────────────


def _has_marker(request: pytest.FixtureRequest, *names: str) -> bool:
    return any(request.node.get_closest_marker(name) is not None for name in names)


@pytest.fixture(autouse=True)
def mock_httpx(request: pytest.FixtureRequest):
    """Mock the httpx async client to block all real HTTP requests."""
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
    """Mock the Redis client to avoid connecting to a real Redis."""
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
    from core.app_context import get_default_app_context

    context = get_default_app_context()
    assert context is not None
    yield context.config


@pytest.fixture
def inline_webhook_task_runner(monkeypatch: pytest.MonkeyPatch):
    """Run the TaskIQ webhook task body directly for request-path integration tests."""
    from services.operations.tasks import process_webhook_task
    from services.webhooks.pipeline import handle_webhook_ingest

    async def run_task_inline(
        client_ip: str | None = None,
        source_name: str | None = None,
        raw_headers: dict[str, str] | None = None,
        raw_body: str | None = None,
        request_id: str | None = None,
        received_at: str | None = None,
        ingest_retry_count: int = 0,
        traceparent: str | None = None,
    ) -> None:
        await handle_webhook_ingest(
            source=source_name or "unknown",
            raw_headers=raw_headers or {},
            raw_body=raw_body or "",
            client_ip=client_ip or "",
            request_id=request_id,
            received_at=received_at,
        )

    monkeypatch.setattr(process_webhook_task, "kiq", run_task_inline)
    return run_task_inline

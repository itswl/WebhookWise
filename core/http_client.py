from __future__ import annotations

import httpx

from core.config import UnifiedConfigManager
from core.logger import get_logger
from core.observability.tracing import inject_trace_headers

logger = get_logger("http_client")


async def _inject_trace_headers(request: httpx.Request) -> None:
    inject_trace_headers(request.headers)


def build_http_client(
    config: UnifiedConfigManager | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    if config is None:
        from core.app_context import get_config_manager

        config = get_config_manager()
    return httpx.AsyncClient(
        timeout=httpx.Timeout(config.retry.FORWARD_TIMEOUT, connect=10.0),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        follow_redirects=False,
        trust_env=False,
        transport=transport,
        event_hooks={"request": [_inject_trace_headers]},
    )


def get_http_client() -> httpx.AsyncClient:
    """Return the AsyncClient owned by the current AppContext."""
    from core.app_context import get_default_app_context

    context = get_default_app_context()
    if context is None:
        raise RuntimeError("default AppContext is not initialized")
    if context.http_client is None or context.http_client.is_closed:
        context.http_client = build_http_client(context.config)
        logger.info("[HTTP] 成功初始化上下文异步客户端")
    return context.http_client

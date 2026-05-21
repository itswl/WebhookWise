import threading

import httpx

from core.config import Config, UnifiedConfigManager
from core.logger import logger
from core.observability.tracing import build_traceparent, get_current_trace_id

_http_client_lock = threading.RLock()


async def _inject_trace_headers(request: httpx.Request) -> None:
    tid = get_current_trace_id()
    if not tid:
        return
    if "X-Request-Id" not in request.headers:
        request.headers["X-Request-Id"] = tid
    if "traceparent" not in request.headers:
        request.headers["traceparent"] = build_traceparent(tid)


def build_http_client(
    config: UnifiedConfigManager = Config,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(config.forwarding.FORWARD_TIMEOUT, connect=10.0),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        follow_redirects=False,
        trust_env=False,
        transport=transport,
        event_hooks={"request": [_inject_trace_headers]},
    )


def get_http_client() -> httpx.AsyncClient:
    """Return the AsyncClient owned by the current AppContext."""
    from core.app_context import get_or_create_default_app_context

    context = get_or_create_default_app_context()
    with _http_client_lock:
        if context.http_client is None or context.http_client.is_closed:
            context.http_client = build_http_client(context.config)
            logger.info("[HTTP] 成功初始化上下文异步客户端")
        return context.http_client


async def close_http_client() -> None:
    """Close the AsyncClient owned by the current AppContext."""
    from core.app_context import get_default_app_context

    context = get_default_app_context()
    if context is not None and context.http_client is not None:
        client = context.http_client
        context.http_client = None
        if not client.is_closed:
            await client.aclose()
            logger.info("[HTTP] 已关闭上下文异步客户端")

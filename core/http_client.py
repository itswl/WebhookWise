import httpx

from core.config import Config
from core.logger import logger
from core.trace import build_traceparent, get_trace_id

_async_client: httpx.AsyncClient | None = None


async def _inject_trace_headers(request: httpx.Request) -> None:
    tid = get_trace_id()
    if not tid:
        return
    if "X-Request-Id" not in request.headers:
        request.headers["X-Request-Id"] = tid
    if "traceparent" not in request.headers:
        request.headers["traceparent"] = build_traceparent(tid)


def _build_async_client(transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(Config.ai.FORWARD_TIMEOUT, connect=10.0),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        follow_redirects=True,
        transport=transport,
        event_hooks={"request": [_inject_trace_headers]},
    )


def get_http_client() -> httpx.AsyncClient:
    """获取全局共享的异步 HTTP 客户端（协程安全，自动管理连接池）"""
    global _async_client
    if _async_client is None or _async_client.is_closed:
        _async_client = _build_async_client()
        logger.info("[HTTP] 成功初始化全局异步客户端")
    return _async_client


async def close_http_client():
    """在应用关闭时调用，释放连接池"""
    global _async_client
    if _async_client and not _async_client.is_closed:
        await _async_client.aclose()
        logger.info("[HTTP] 已关闭全局异步客户端")

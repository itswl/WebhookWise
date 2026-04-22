import httpx
from typing import Optional
from core.config import Config
from core.logger import logger

_async_client: Optional[httpx.AsyncClient] = None

def get_http_client() -> httpx.AsyncClient:
    """获取全局共享的异步 HTTP 客户端（协程安全，自动管理连接池）"""
    global _async_client
    if _async_client is None or _async_client.is_closed:
        _async_client = httpx.AsyncClient(
            timeout=httpx.Timeout(Config.FORWARD_TIMEOUT, connect=10.0),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
            follow_redirects=True
        )
        logger.info("[HTTP] 成功初始化全局异步客户端")
    return _async_client

async def close_http_client():
    """在应用关闭时调用，释放连接池"""
    global _async_client
    if _async_client and not _async_client.is_closed:
        await _async_client.aclose()
        logger.info("[HTTP] 已关闭全局异步客户端")

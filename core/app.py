import os
import socket
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from api.admin import admin_router
from api.ai_usage import ai_usage_router
from api.deep_analysis import deep_analysis_router
from api.forward_retry import forward_retry_router
from api.forward_rules import forward_rules_router
from api.reanalysis import reanalysis_router
from api.webhook import webhook_router
from core.auth import verify_api_key
from core.config import Config
from core.http_client import close_http_client, get_http_client
from core.logger import logger, stop_log_listener
from core.metrics import setup_metrics
from core.redis_client import dispose_redis
from core.runtime_config import runtime_config
from db.session import dispose_engine, init_engine
from services.ai_client import reset_openai_client
from services.poller_scheduler import start_scheduler, stop_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    Config.validate()
    if not Config.API_KEY and not (Config.DEBUG or Config.ALLOW_UNAUTHENTICATED_ADMIN):
        raise RuntimeError(
            "API_KEY 未配置且未允许公开管理接口，请设置 API_KEY 或在本地启用 ALLOW_UNAUTHENTICATED_ADMIN=true"
        )
    get_http_client()
    await init_engine()
    # 从数据库加载运行时配置（覆盖 .env 默认值）
    await runtime_config.load_from_db()
    # 启动 Redis Pub/Sub 配置变更监听
    await runtime_config.start_subscriber()
    await start_scheduler()
    yield
    await stop_scheduler()
    # 停止配置变更监听
    await runtime_config.stop_subscriber()
    await dispose_engine()
    await dispose_redis()
    reset_openai_client()  # 释放 OpenAI 单例引用（其底层连接随 close_http_client 一并关闭）
    await close_http_client()
    stop_log_listener()


app = FastAPI(title="Webhook AI Assistant", lifespan=lifespan)


setup_metrics(app)
app.add_middleware(GZipMiddleware, minimum_size=500)
app.mount("/static", StaticFiles(directory="templates/static"), name="static")


class SecurityHeadersMiddleware:
    """Pure ASGI middleware – avoids BaseHTTPMiddleware's TaskGroup isolation
    that breaks asyncpg connections across tasks."""

    _EXTRA_HEADERS = [
        (b"x-content-type-options", b"nosniff"),
        (b"x-frame-options", b"DENY"),
        (b"referrer-policy", b"no-referrer"),
    ]

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: dict) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                existing_names = {h[0] for h in headers}
                for name, value in self._EXTRA_HEADERS:
                    if name not in existing_names:
                        headers.append((name, value))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


app.add_middleware(SecurityHeadersMiddleware)


_WORKER_ID = f"{socket.gethostname()}-{os.getpid()}"
logger.debug(f"worker_id={_WORKER_ID}")


app.include_router(deep_analysis_router, dependencies=[Depends(verify_api_key)])
app.include_router(forward_retry_router, dependencies=[Depends(verify_api_key)])
app.include_router(forward_rules_router, dependencies=[Depends(verify_api_key)])
app.include_router(reanalysis_router, dependencies=[Depends(verify_api_key)])
app.include_router(ai_usage_router, dependencies=[Depends(verify_api_key)])
app.include_router(admin_router, dependencies=[Depends(verify_api_key)])
app.include_router(webhook_router)

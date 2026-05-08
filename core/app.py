import asyncio
import os
import socket
from collections.abc import AsyncIterator, MutableMapping
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

# 必须导入任务以注册到 broker
import services.operations.tasks  # noqa: F401
from adapters.ecosystem_adapters import initialize_adapters
from api.admin import admin_router
from api.ai_usage import ai_usage_router
from api.deep_analysis import deep_analysis_router
from api.forwarding import forwarding_router
from api.reanalysis import reanalysis_router
from api.webhook import webhook_router
from core.auth import verify_api_key
from core.config import Config
from core.http_client import close_http_client, get_http_client
from core.logger import logger, stop_log_listener
from core.metrics import setup_metrics
from core.otel import setup_otel
from core.redis_client import dispose_redis
from core.taskiq_broker import broker
from core.trace import build_traceparent, extract_trace_id_from_headers, generate_trace_id, set_trace_id, trace_id_var
from db.session import dispose_engine, init_engine
from services.analysis.ai_analyzer import initialize_openai_client, reset_openai_client
from services.webhooks.pipeline import get_running_tasks


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    if not Config.security.API_KEY and not (Config.server.DEBUG or Config.security.ALLOW_UNAUTHENTICATED_ADMIN):
        raise RuntimeError(
            "API_KEY 未配置且未允许公开管理接口，请设置 API_KEY 或在本地启用 ALLOW_UNAUTHENTICATED_ADMIN=true"
        )
    get_http_client()
    initialize_adapters()
    await init_engine()
    if Config.server.ENABLE_RUNTIME_CONFIG:
        await Config.load_from_db()
        await Config.start_subscriber()
    if Config.ai.ENABLE_AI_ANALYSIS and Config.ai.OPENAI_API_KEY:
        await initialize_openai_client()

    # 启动 TaskIQ Broker (API 侧只需 startup)
    await broker.startup()

    yield

    await Config.stop_subscriber()
    await broker.shutdown()

    # 优雅等待正在运行的任务
    running = get_running_tasks()
    if running:
        grace_timeout = Config.server.GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS
        logger.info(
            "优雅停机：等待 %d 个正在运行的任务完成 (超时 %ds)",
            len(running),
            grace_timeout,
        )
        await asyncio.wait(running, timeout=grace_timeout)

    await dispose_engine()
    await dispose_redis()
    await reset_openai_client()
    await close_http_client()
    stop_log_listener()


app = FastAPI(title="Webhook AI Assistant", lifespan=lifespan)


setup_metrics(app)
setup_otel(app)
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

        async def send_with_headers(message: MutableMapping[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                existing_names = {h[0].lower() for h in headers}
                for name, value in self._EXTRA_HEADERS:
                    if name.lower() not in existing_names:
                        headers.append((name, value))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


app.add_middleware(SecurityHeadersMiddleware)


class TraceContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin1").lower(): v.decode("latin1") for k, v in scope.get("headers") or []}
        incoming = extract_trace_id_from_headers(headers)
        if incoming and "traceparent" not in headers:
            raw_headers = list(scope.get("headers") or [])
            raw_headers.append((b"traceparent", build_traceparent(incoming).encode("latin1")))
            scope["headers"] = raw_headers

        # 优先使用 OTEL 当前 span 的 trace_id 保证日志与 APM 双向关联；
        # OTEL 未启用时回退到请求头携带的 trace_id 或生成新 id
        from core.otel import get_otel_trace_id

        otel_tid = get_otel_trace_id()
        token = set_trace_id(otel_tid or incoming or generate_trace_id())
        try:
            await self.app(scope, receive, send)
            # 请求处理完成后，OTEL span 已激活，同步 trace_id 到日志上下文
            otel_tid = get_otel_trace_id()
            if otel_tid:
                set_trace_id(otel_tid)
        finally:
            trace_id_var.reset(token)


app.add_middleware(TraceContextMiddleware)


_WORKER_ID = f"{socket.gethostname()}-{os.getpid()}"
logger.debug(f"worker_id={_WORKER_ID}")


app.include_router(deep_analysis_router, dependencies=[Depends(verify_api_key)])
app.include_router(reanalysis_router, dependencies=[Depends(verify_api_key)])
app.include_router(ai_usage_router, dependencies=[Depends(verify_api_key)])
app.include_router(forwarding_router, dependencies=[Depends(verify_api_key)])
app.include_router(admin_router, dependencies=[Depends(verify_api_key)])
app.include_router(webhook_router)

import os
import socket
import time
from collections.abc import AsyncIterator, Callable, MutableMapping
from contextlib import asynccontextmanager
from typing import Any, cast

from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

# Full 模式需要导入任务以注册到 broker；Lite 模式复用其中的纯函数处理入口。
import services.operations.tasks  # noqa: F401
from adapters.ecosystem_adapters import initialize_adapters
from api.admin import admin_router
from api.ai_usage import ai_usage_router
from api.deep_analysis import deep_analysis_router
from api.forwarding import forwarding_router
from api.reanalysis import reanalysis_router
from api.webhook import webhook_router
from core.auth import verify_api_key
from core.config import UnifiedConfigManager
from core.dependencies import get_config_manager
from core.http_client import close_http_client, get_http_client
from core.log_context import clear_log_context, set_log_context
from core.logger import logger, stop_log_listener
from core.metrics import setup_metrics
from core.otel import setup_otel
from core.redis_client import dispose_redis
from core.runtime_mode import is_lite_mode, uses_taskiq_broker
from core.taskiq_broker import broker
from core.trace import build_traceparent, extract_trace_id_from_headers, generate_trace_id, set_trace_id, trace_id_var
from db.session import dispose_engine, init_engine
from services.analysis.ai_analyzer import initialize_openai_client, reset_openai_client

_PLACEHOLDER_SECRETS = {"change-me", "changeme", "replace-me", "please-change", "please-change-me"}


def _app_config(app: FastAPI) -> UnifiedConfigManager:
    config = getattr(app.state, "config_manager", None)
    return cast(UnifiedConfigManager, config) if config is not None else get_config_manager()


def _looks_like_placeholder_secret(value: str) -> bool:
    normalized = value.strip().lower()
    return normalized in _PLACEHOLDER_SECRETS or normalized.startswith("please-change-")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    config = _app_config(app)
    lite_mode = is_lite_mode(config)
    logger.info(
        "[App] 启动中 env=%s debug=%s run_mode=%s runtime_config=%s ai_enabled=%s",
        os.getenv("APP_ENV", "production"),
        config.server.DEBUG,
        config.server.RUN_MODE,
        config.server.ENABLE_RUNTIME_CONFIG,
        config.ai.ENABLE_AI_ANALYSIS,
    )
    if not config.security.API_KEY and not (config.server.DEBUG or config.security.ALLOW_UNAUTHENTICATED_ADMIN):
        raise RuntimeError(
            "API_KEY 未配置且未允许公开管理接口，请设置 API_KEY 或在本地启用 ALLOW_UNAUTHENTICATED_ADMIN=true"
        )
    if os.getenv("APP_ENV", "production") == "production" and _looks_like_placeholder_secret(config.security.API_KEY):
        raise RuntimeError("API_KEY 仍是示例占位值，请替换为真实随机密钥")
    if (
        os.getenv("APP_ENV", "production") == "production"
        and config.security.ADMIN_WRITE_KEY
        and _looks_like_placeholder_secret(config.security.ADMIN_WRITE_KEY)
    ):
        raise RuntimeError("ADMIN_WRITE_KEY 仍是示例占位值，请替换为真实随机密钥")
    if (
        os.getenv("APP_ENV", "production") == "production"
        and not config.security.REQUIRE_WEBHOOK_AUTH
        and not config.security.ALLOW_UNAUTHENTICATED_WEBHOOK
    ):
        raise RuntimeError(
            "生产环境未开启 Webhook 鉴权。请设置 REQUIRE_WEBHOOK_AUTH=true 和 WEBHOOK_SECRET，"
            "或显式设置 ALLOW_UNAUTHENTICATED_WEBHOOK=true 承担公开接收风险"
        )
    if (
        os.getenv("APP_ENV", "production") == "production"
        and config.security.REQUIRE_WEBHOOK_AUTH
        and _looks_like_placeholder_secret(config.security.WEBHOOK_SECRET)
    ):
        raise RuntimeError("WEBHOOK_SECRET 仍是示例占位值，请替换为真实随机密钥")
    app.state.http_client = get_http_client()
    initialize_adapters()
    await init_engine()
    if config.server.ENABLE_RUNTIME_CONFIG:
        await config.load_from_db()
        if not lite_mode:
            await config.start_subscriber()
        else:
            logger.info("[App] lite 模式跳过 Redis Pub/Sub 配置同步")
    if config.ai.ENABLE_AI_ANALYSIS and config.ai.OPENAI_API_KEY:
        await initialize_openai_client(http_client=app.state.http_client)

    if uses_taskiq_broker(config):
        # 启动 TaskIQ Broker (API 侧只需 startup)
        await broker.startup()
    else:
        from services.operations.lite_runtime import start_lite_runtime

        app.state.lite_runtime = start_lite_runtime()
    logger.info("[App] 启动完成 port=%s worker_id=%s", config.server.PORT, _WORKER_ID)

    try:
        yield
    finally:
        logger.info("[App] 正在关闭 worker_id=%s", _WORKER_ID)

        await config.stop_subscriber()
        if uses_taskiq_broker(config):
            await broker.shutdown()
        else:
            from services.operations.lite_runtime import stop_lite_runtime

            lite_runtime = getattr(app.state, "lite_runtime", None)
            if lite_runtime is not None:
                stop_event, tasks = lite_runtime
                await stop_lite_runtime(stop_event, tasks)

        await dispose_engine()
        await dispose_redis()
        await reset_openai_client()
        await close_http_client()
        logger.info("[App] 关闭完成 worker_id=%s", _WORKER_ID)
        stop_log_listener()


app = FastAPI(title="Webhook AI Assistant", lifespan=lifespan)
app.state.config_manager = get_config_manager()


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


class RequestBodyLimitExceeded(Exception):
    pass


class RequestBodyLimitMiddleware:
    def __init__(self, app: ASGIApp, max_body_bytes_provider: Callable[[], int]) -> None:
        self.app = app
        self.max_body_bytes_provider = max_body_bytes_provider

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        max_bytes = max(0, int(self.max_body_bytes_provider() or 0))
        if max_bytes <= 0:
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin1").lower(): v.decode("latin1") for k, v in scope.get("headers") or []}
        content_length = headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > max_bytes:
                    await self._send_413(send, max_bytes)
                    return
            except ValueError:
                pass

        seen = 0

        async def limited_receive() -> MutableMapping[str, Any]:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                body = message.get("body", b"")
                seen += len(body) if isinstance(body, bytes) else 0
                if seen > max_bytes:
                    raise RequestBodyLimitExceeded
            return message

        try:
            await self.app(scope, limited_receive, send)
        except RequestBodyLimitExceeded:
            await self._send_413(send, max_bytes)

    async def _send_413(self, send: Send, max_bytes: int) -> None:
        body = f'{{"success":false,"error":"Payload too large","max_bytes":{max_bytes}}}'.encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("latin1")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


app.add_middleware(
    RequestBodyLimitMiddleware,
    max_body_bytes_provider=lambda: _app_config(app).security.MAX_WEBHOOK_BODY_BYTES,
)


class TraceContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin1").lower(): v.decode("latin1") for k, v in scope.get("headers") or []}
        method = str(scope.get("method") or "")
        path = str(scope.get("path") or "")
        client = scope.get("client")
        client_ip = client[0] if isinstance(client, tuple) and client else ""
        content_length = headers.get("content-length", "")
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
        clear_log_context()
        set_log_context(request_id=trace_id_var.get())
        started_at = time.perf_counter()
        status_code = 500

        async def send_with_status(message: MutableMapping[str, Any]) -> None:
            nonlocal status_code
            if message.get("type") == "http.response.start":
                status_code = int(message.get("status") or 0)
            await send(message)

        try:
            await self.app(scope, receive, send_with_status)
            # 请求处理完成后，OTEL span 已激活，同步 trace_id 到日志上下文
            otel_tid = get_otel_trace_id()
            if otel_tid:
                set_trace_id(otel_tid)
        except Exception:
            duration_ms = int((time.perf_counter() - started_at) * 1000)
            logger.exception(
                "[HTTP] 请求异常 method=%s path=%s status=%s duration=%dms ip=%s content_length=%s",
                method,
                path,
                status_code,
                duration_ms,
                client_ip,
                content_length,
            )
            raise
        finally:
            if path not in {"/live", "/ready", "/health", "/metrics"} and not path.startswith("/static/"):
                duration_ms = int((time.perf_counter() - started_at) * 1000)
                logger.info(
                    "[HTTP] 请求完成 method=%s path=%s status=%s duration=%dms ip=%s content_length=%s",
                    method,
                    path,
                    status_code,
                    duration_ms,
                    client_ip,
                    content_length,
                )
            clear_log_context()
            trace_id_var.reset(token)


app.add_middleware(TraceContextMiddleware)


_WORKER_ID = f"{socket.gethostname()}-{os.getpid()}"
logger.debug("worker_id=%s", _WORKER_ID)


app.include_router(deep_analysis_router, dependencies=[Depends(verify_api_key)])
app.include_router(reanalysis_router, dependencies=[Depends(verify_api_key)])
app.include_router(ai_usage_router, dependencies=[Depends(verify_api_key)])
app.include_router(forwarding_router, dependencies=[Depends(verify_api_key)])
app.include_router(admin_router, dependencies=[Depends(verify_api_key)])
app.include_router(webhook_router)

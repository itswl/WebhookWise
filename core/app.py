import os
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles

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
from core.logger import logger, stop_log_listener
from core.observability import setup_observability
from core.redis_client import dispose_redis
from core.runtime_mode import is_lite_mode, uses_taskiq_broker
from core.taskiq_broker import broker
from core.web.middleware import RequestBodyLimitMiddleware, SecurityHeadersMiddleware, TraceContextMiddleware
from core.web.startup_checks import validate_startup_security
from db.session import dispose_engine, init_engine
from services.analysis.ai_analyzer import initialize_openai_client, reset_openai_client


def _app_config(app: FastAPI) -> UnifiedConfigManager:
    config = getattr(app.state, "config_manager", None)
    return cast(UnifiedConfigManager, config) if config is not None else get_config_manager()


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
    validate_startup_security(config)
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


setup_observability(app)
app.mount("/static", StaticFiles(directory="templates/static"), name="static")


app.add_middleware(SecurityHeadersMiddleware)


app.add_middleware(
    RequestBodyLimitMiddleware,
    max_body_bytes_provider=lambda: _app_config(app).security.MAX_WEBHOOK_BODY_BYTES,
)


app.add_middleware(TraceContextMiddleware)


_WORKER_ID = f"{socket.gethostname()}-{os.getpid()}"
logger.debug("worker_id=%s", _WORKER_ID)


app.include_router(deep_analysis_router, dependencies=[Depends(verify_api_key)])
app.include_router(reanalysis_router, dependencies=[Depends(verify_api_key)])
app.include_router(ai_usage_router, dependencies=[Depends(verify_api_key)])
app.include_router(forwarding_router, dependencies=[Depends(verify_api_key)])
app.include_router(admin_router, dependencies=[Depends(verify_api_key)])
app.include_router(webhook_router)

import os
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles

from api.admin import admin_router
from api.ai_usage import ai_usage_router
from api.deep_analysis import deep_analysis_router
from api.forwarding import forwarding_router
from api.reanalysis import reanalysis_router
from api.runtime_wiring import install_runtime_lifecycle_hooks
from api.webhook import webhook_router
from core.app_context import AppContext, get_default_app_context, init_default_app_context, set_default_app_context
from core.auth import verify_api_key
from core.config import UnifiedConfigManager
from core.logger import get_logger, stop_log_listener
from core.observability import setup_observability, shutdown_observability
from core.service_lifecycle import start_runtime_services, stop_runtime_services
from core.taskiq_broker import broker
from core.web.middleware import RequestBodyLimitMiddleware, SecurityHeadersMiddleware, TraceContextMiddleware
from core.web.startup_checks import validate_startup_security

logger = get_logger("app")


def _app_config(app: FastAPI) -> UnifiedConfigManager:
    return _app_context(app).config


def _app_context(app: FastAPI) -> AppContext:
    context = getattr(app.state, "app_context", None)
    if not isinstance(context, AppContext):
        context = get_default_app_context() or init_default_app_context(UnifiedConfigManager())
        app.state.app_context = context
    return context


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    context = _app_context(app)
    set_default_app_context(context)
    config = context.config
    logger.info(
        "[App] 启动中 env=%s debug=%s run_mode=%s ai_enabled=%s",
        config.server.APP_ENV,
        config.server.DEBUG,
        config.server.RUN_MODE,
        config.ai.ENABLE_AI_ANALYSIS,
    )
    validate_startup_security(config)
    install_runtime_lifecycle_hooks()
    services = await start_runtime_services(
        config,
        context=context,
        broker=broker,
        start_broker=True,
        initialize_ai_client=True,
    )
    app.state.app_context = services.app_context
    logger.info("[App] 启动完成 port=%s worker_id=%s", config.server.PORT, _WORKER_ID)

    try:
        yield
    finally:
        logger.info("[App] 正在关闭 worker_id=%s", _WORKER_ID)
        await stop_runtime_services(
            config,
            context=context,
            broker=broker,
            stop_broker=True,
            reset_ai_client=True,
        )
        logger.info("[App] 关闭完成 worker_id=%s", _WORKER_ID)
        shutdown_observability()
        stop_log_listener()


app = FastAPI(title="Webhook AI Assistant", lifespan=lifespan)
app.state.app_context = get_default_app_context() or init_default_app_context(UnifiedConfigManager())


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

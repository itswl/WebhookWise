from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.gzip import GZipMiddleware

from adapters.ecosystem_adapters import initialize_adapters
from api import internal_error_response
from api.dashboard import ImmutableStaticFiles, dashboard_router
from api.health import health_router
from api.v1.router import v1_router
from core.app_context import AppContext, get_default_app_context, init_default_app_context, set_default_app_context
from core.logger import get_logger, stop_log_listener
from core.observability import setup_observability, shutdown_observability
from core.service_lifecycle import start_runtime_services, stop_runtime_services
from core.taskiq_broker import broker
from core.web.middleware import RequestBodyLimitMiddleware, SecurityHeadersMiddleware, TraceContextMiddleware
from core.web.startup_checks import validate_startup_security
from services.analysis.ai_llm_client import initialize_openai_client, reset_openai_client

logger = get_logger("app")


def _app_context(app: FastAPI) -> AppContext:
    context = getattr(app.state, "app_context", None)
    if not isinstance(context, AppContext):
        context = get_default_app_context() or init_default_app_context()
        app.state.app_context = context
    return context


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    context = _app_context(app)
    set_default_app_context(context)
    config = context.config
    logger.info(
        "[App] Starting up env=%s debug=%s run_mode=%s ai_enabled=%s",
        config.server.APP_ENV,
        config.server.DEBUG,
        config.server.RUN_MODE,
        config.ai.ENABLE_AI_ANALYSIS,
    )

    validate_startup_security(config)
    services = await start_runtime_services(
        config,
        context=context,
        broker=broker,
        start_broker=True,
        initialize_adapter_registry=True,
        initialize_adapter_registry_hook=initialize_adapters,
        initialize_ai_client=True,
        initialize_ai_client_hook=initialize_openai_client,
    )
    app.state.app_context = services.app_context

    from services.forwarding.rules import start_rules_invalidation_listener, stop_rules_invalidation_listener
    from services.silences.store import start_silences_invalidation_listener, stop_silences_invalidation_listener

    await start_rules_invalidation_listener()
    await start_silences_invalidation_listener()

    async with AsyncExitStack() as lifecycle:
        # The mounted MCP app is a sub-app; Starlette does not run a mounted
        # app's lifespan, so its Streamable-HTTP session manager must be driven
        # from here. Only when the read-only MCP server is enabled.
        if config.security.MCP_ENABLED:
            from api.mcp import mcp_server

            await lifecycle.enter_async_context(mcp_server.session_manager.run())

        logger.info("[App] Startup complete port=%s worker_id=%s", config.server.PORT, _WORKER_ID)
        try:
            yield
        finally:
            logger.info("[App] Shutting down worker_id=%s", _WORKER_ID)
            # Buffered AI-usage rows must land before the DB engine goes away.
            from services.analysis.ai_usage import flush_ai_usage

            await flush_ai_usage()
            await stop_rules_invalidation_listener()
            await stop_silences_invalidation_listener()
            await stop_runtime_services(
                config,
                context=context,
                broker=broker,
                stop_broker=True,
                reset_ai_client=True,
                reset_ai_client_hook=reset_openai_client,
            )
            logger.info("[App] Shutdown complete worker_id=%s", _WORKER_ID)
            shutdown_observability()
            stop_log_listener()


app = FastAPI(title="Webhook AI Assistant", lifespan=lifespan, debug=False)
app.state.app_context = get_default_app_context() or init_default_app_context()


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error("[App] Unhandled exception path=%s error=%s", request.url.path, exc, exc_info=True)
    return internal_error_response()


setup_observability(app)
app.mount("/static", ImmutableStaticFiles(directory="templates/static"), name="static")

app.add_middleware(SecurityHeadersMiddleware)

# Compress text responses (HTML/CSS/JS/JSON). minimum_size avoids the overhead
# on tiny payloads. Added after SecurityHeadersMiddleware so it compresses the
# fully-headed response; static assets are compressed on the fly too.
app.add_middleware(GZipMiddleware, minimum_size=1024)

app.add_middleware(
    RequestBodyLimitMiddleware,
    max_body_bytes_provider=lambda: _app_context(app).config.security.MAX_WEBHOOK_BODY_BYTES,
)

app.add_middleware(TraceContextMiddleware)

_WORKER_ID = _app_context(app).config.server.WORKER_ID
logger.debug("worker_id=%s", _WORKER_ID)

app.include_router(health_router)
app.include_router(dashboard_router)
app.include_router(v1_router)

# Read-only MCP (Streamable HTTP) server at /mcp, guarded by the management API
# key. Its session manager lifecycle is driven from the lifespan above. Opt-in
# via SECURITY__MCP_ENABLED so the endpoint is not exposed unless configured.
if _app_context(app).config.security.MCP_ENABLED:
    from api.mcp import build_mcp_app  # noqa: E402

    app.mount("/mcp", build_mcp_app())
    logger.info("[App] Read-only MCP server mounted at /mcp")

import os
import socket
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles

from core.auth import verify_api_key
from core.config import Config
from core.http_client import close_http_client, get_http_client
from core.logger import logger
from core.metrics import setup_metrics
from core.pipeline import _decide_forwarding, handle_webhook_process
from core.pollers import start_background_pollers
from core.routes.admin import admin_router
from core.routes.ai_usage import ai_usage_router
from core.routes.deep_analysis import deep_analysis_router
from core.routes.forward_rules import forward_rules_router
from core.routes.reanalysis import reanalysis_router
from core.routes.webhook import webhook_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    Config.validate()
    if not Config.API_KEY and not (Config.DEBUG or Config.ALLOW_UNAUTHENTICATED_ADMIN):
        raise RuntimeError("API_KEY 未配置且未允许公开管理接口，请设置 API_KEY 或在本地启用 ALLOW_UNAUTHENTICATED_ADMIN=true")
    get_http_client()
    start_background_pollers()
    yield
    await close_http_client()


app = FastAPI(title="Webhook AI Assistant", lifespan=lifespan)


setup_metrics(app)
app.add_middleware(GZipMiddleware, minimum_size=500)
app.mount("/static", StaticFiles(directory="templates/static"), name="static")


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


_WORKER_ID = f"{socket.gethostname()}-{os.getpid()}"
logger.debug(f"worker_id={_WORKER_ID}")


app.include_router(deep_analysis_router, dependencies=[Depends(verify_api_key)])
app.include_router(forward_rules_router, dependencies=[Depends(verify_api_key)])
app.include_router(reanalysis_router, dependencies=[Depends(verify_api_key)])
app.include_router(ai_usage_router, dependencies=[Depends(verify_api_key)])
app.include_router(admin_router, dependencies=[Depends(verify_api_key)])
app.include_router(webhook_router)

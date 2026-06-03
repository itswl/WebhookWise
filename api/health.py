"""Non-versioned health check routes."""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from core.redis_client import redis_ping
from db.engine import test_db_connection
from schemas.webhook import HealthResponse

health_router = APIRouter()


@health_router.get("/live", response_model=HealthResponse)
async def liveness_check() -> JSONResponse:
    """进程存活检查，不触碰外部依赖。"""
    return JSONResponse(content={"success": True, "data": {"status": "alive"}}, status_code=200)


@health_router.get("/ready", response_model=HealthResponse)
async def readiness_check() -> JSONResponse:
    """就绪检查：API 依赖 DB 与 Redis 队列。"""
    db_ok = await test_db_connection()
    redis_ok = await redis_ping()
    ready = db_ok and redis_ok
    content = {
        "success": True,
        "data": {
            "status": "ready" if ready else "unready",
            "database": "ok" if db_ok else "failed",
            "redis": "ok" if redis_ok else "failed",
            "queue": "redis_stream",
        },
    }
    return JSONResponse(content=content, status_code=200 if ready else 503)

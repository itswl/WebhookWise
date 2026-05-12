"""
api/webhook.py
=====================
Webhook 接收 + 健康检查 + Dashboard + Webhooks API 路由。
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth import verify_api_key
from core.logger import logger
from core.metrics import WEBHOOK_RECEIVED_TOTAL, sanitize_source
from core.redis_client import redis_ping
from core.sensitive_data import redact_event_dict
from core.trace import generate_trace_id, set_trace_id
from core.webhook_security import check_rate_limit_dep, verify_webhook_auth_dep
from db.session import get_db_session, test_db_connection
from models import WebhookEvent
from schemas import HealthResponse, WebhookListResponse, WebhookReceiveResponse
from services.operations.tasks import process_webhook_task
from services.webhooks.command_service import (
    get_client_ip,
    quick_receive_webhook,
)
from services.webhooks.policies import WebhookReceivePolicy
from services.webhooks.query_service import list_webhook_summaries

webhook_router = APIRouter()

JSONDict = dict[str, Any]
MAX_SOURCE_LENGTH = 100


def _normalize_source_hint(value: str | None) -> str:
    source = str(value or "").strip() or "unknown"
    if len(source) > MAX_SOURCE_LENGTH:
        raise HTTPException(status_code=400, detail=f"source must be at most {MAX_SOURCE_LENGTH} characters")
    return source


def _payload_too_large_response(
    raw_body: bytes, source_hint: str, *, policy: WebhookReceivePolicy | None = None
) -> JSONResponse | None:
    policy = policy or WebhookReceivePolicy.from_config()
    if policy.max_body_bytes and len(raw_body) > policy.max_body_bytes:
        WEBHOOK_RECEIVED_TOTAL.labels(source=sanitize_source(source_hint), status="rejected_size").inc()
        return JSONResponse(status_code=413, content={"success": False, "error": "Payload too large"})
    return None


# ── 基础路由 ───────────────────────────────────────────────────────────────────


@webhook_router.get("/health", response_model=HealthResponse)
async def health_check() -> JSONResponse:
    """兼容性健康检查：等同 readiness。"""
    return await readiness_check()


@webhook_router.get("/live", response_model=HealthResponse)
async def liveness_check() -> JSONResponse:
    """进程存活检查，不触碰外部依赖。"""
    return JSONResponse(content={"success": True, "data": {"status": "alive"}}, status_code=200)


@webhook_router.get("/ready", response_model=HealthResponse)
async def readiness_check() -> JSONResponse:
    """就绪检查：接收链路依赖 DB 与 Redis 队列。"""
    db_ok = await test_db_connection()
    redis_ok = await redis_ping()
    ready = db_ok and redis_ok
    content = {
        "success": True,
        "data": {
            "status": "ready" if ready else "unready",
            "database": "ok" if db_ok else "failed",
            "redis": "ok" if redis_ok else "failed",
        },
    }
    return JSONResponse(content=content, status_code=200 if ready else 503)


@webhook_router.get("/")
@webhook_router.get("/dashboard")
async def dashboard() -> FileResponse:
    """返回 Dashboard 页面。"""
    return FileResponse("templates/dashboard.html")


# ── Webhook 接收 ───────────────────────────────────────────────────────────────


@webhook_router.post(
    "/webhook",
    dependencies=[Depends(check_rate_limit_dep), Depends(verify_webhook_auth_dep)],
    response_model=WebhookReceiveResponse,
    status_code=202,
)
async def receive_webhook(
    request: Request,
    source: str | None = Query(None, max_length=MAX_SOURCE_LENGTH),
    session: AsyncSession = Depends(get_db_session),
) -> JSONDict | JSONResponse:
    """通用 Webhook 接收入口。"""
    tid = generate_trace_id()
    set_trace_id(tid)
    source_hint = _normalize_source_hint(source or request.headers.get("x-webhook-source"))

    raw_body = await request.body()
    if too_large_response := _payload_too_large_response(raw_body, source_hint):
        return too_large_response

    client_ip = get_client_ip(request)
    headers = dict(request.headers)
    raw_body_str = raw_body.decode("utf-8", errors="replace")

    event_id = await quick_receive_webhook(
        session=session,
        source=source_hint,
        raw_headers=headers,
        raw_body=raw_body_str,
    )
    await session.commit()
    set_trace_id(generate_trace_id(event_id=event_id))
    logger.info(
        "[Webhook] 已接收 event_id=%s source=%s ip=%s size=%d",
        event_id,
        source_hint,
        client_ip,
        len(raw_body),
    )

    await process_webhook_task.kiq(event_id=event_id, client_ip=client_ip or "")

    return {"success": True, "message": "Webhook received and queued for processing", "event_id": event_id}


@webhook_router.post(
    "/webhook/{source}",
    dependencies=[Depends(check_rate_limit_dep), Depends(verify_webhook_auth_dep)],
    response_model=WebhookReceiveResponse,
    status_code=202,
)
async def receive_webhook_with_source(
    request: Request,
    source: str = Path(..., max_length=MAX_SOURCE_LENGTH),
    session: AsyncSession = Depends(get_db_session),
) -> JSONDict | JSONResponse:
    """带来源标识的 Webhook 接收入口。"""
    tid = generate_trace_id()
    set_trace_id(tid)
    source_hint = _normalize_source_hint(source)

    raw_body = await request.body()
    if too_large_response := _payload_too_large_response(raw_body, source_hint):
        return too_large_response

    client_ip = get_client_ip(request)
    headers = dict(request.headers)
    raw_body_str = raw_body.decode("utf-8", errors="replace")

    event_id = await quick_receive_webhook(
        session=session,
        source=source_hint,
        raw_headers=headers,
        raw_body=raw_body_str,
    )
    await session.commit()
    set_trace_id(generate_trace_id(event_id=event_id))
    logger.info("[Webhook] 已接收 event_id=%s source=%s ip=%s size=%d", event_id, source_hint, client_ip, len(raw_body))

    await process_webhook_task.kiq(event_id=event_id, client_ip=client_ip or "")

    return {"success": True, "message": "Webhook received and queued for processing", "event_id": event_id}


# ── 查询路由 ───────────────────────────────────────────────────────────────────


@webhook_router.get("/api/webhooks", dependencies=[Depends(verify_api_key)], response_model=WebhookListResponse)
async def get_webhooks_endpoint(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=500),
    cursor: int | None = Query(None, alias="cursor_id"),
    importance: str = Query(""),
    source: str = Query(""),
    session: AsyncSession = Depends(get_db_session),
) -> JSONDict:
    """获取所有 webhook 事件的摘要列表。"""
    items, has_more, next_cursor = await list_webhook_summaries(
        session, cursor_id=cursor, importance=importance, source=source, page_size=page_size
    )
    return {
        "success": True,
        "data": items,
        "pagination": {"next_cursor": next_cursor, "has_more": has_more, "page_size": page_size},
    }


@webhook_router.get("/api/webhooks/{webhook_id}", dependencies=[Depends(verify_api_key)], response_model=None)
async def get_webhook_detail_endpoint(
    webhook_id: int, session: AsyncSession = Depends(get_db_session)
) -> JSONDict | JSONResponse:
    """获取单个 webhook 事件的详细信息。"""
    event = await session.get(WebhookEvent, webhook_id)
    if not event:
        return JSONResponse(status_code=404, content={"success": False, "error": "Webhook not found"})

    return {"success": True, "data": redact_event_dict(event.to_dict())}

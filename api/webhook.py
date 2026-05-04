"""
api/webhook.py
=====================
Webhook 接收 + 健康检查 + Dashboard + Webhooks API 路由。
"""

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import Config
from core.trace import generate_trace_id, set_trace_id
from core.webhook_security import check_rate_limit_dep, verify_webhook_auth_dep
from db.session import get_db_session, test_db_connection
from models import WebhookEvent
from schemas import HealthResponse, WebhookDetailResponse, WebhookListResponse, WebhookReceiveResponse
from services.tasks import process_webhook_task
from services.webhook_orchestrator import (
    get_client_ip,
    list_webhook_summaries,
    quick_receive_webhook,
)

webhook_router = APIRouter()


# ── 基础路由 ───────────────────────────────────────────────────────────────────


@webhook_router.get("/health", response_model=HealthResponse)
async def health_check():
    """健康检查接口。"""
    db_ok = await test_db_connection()
    return {"success": True, "data": {"status": "ok", "database": "up" if db_ok else "down"}}


@webhook_router.get("/")
@webhook_router.get("/dashboard")
async def dashboard():
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
    source: str | None = Query(None),
    session: AsyncSession = Depends(get_db_session),
):
    """通用 Webhook 接收入口。"""
    tid = generate_trace_id()
    set_trace_id(tid)

    raw_body = await request.body()
    if Config.security.MAX_WEBHOOK_BODY_BYTES and len(raw_body) > Config.security.MAX_WEBHOOK_BODY_BYTES:
        return JSONResponse(status_code=413, content={"success": False, "error": "Payload too large"})

    client_ip = get_client_ip(request)
    headers = dict(request.headers)
    raw_body_str = raw_body.decode("utf-8", errors="replace")

    event_id = await quick_receive_webhook(
        session=session,
        source=source or headers.get("x-webhook-source", "unknown"),
        raw_headers=headers,
        raw_body=raw_body_str,
    )
    await session.commit()
    set_trace_id(generate_trace_id(event_id=event_id))

    await process_webhook_task.kiq(event_id=event_id, client_ip=client_ip or "")

    return {"success": True, "message": "Webhook received and queued for processing", "event_id": event_id}


@webhook_router.post(
    "/webhook/{source}",
    dependencies=[Depends(check_rate_limit_dep), Depends(verify_webhook_auth_dep)],
    response_model=WebhookReceiveResponse,
    status_code=202,
)
async def receive_webhook_with_source(
    source: str,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    """带来源标识的 Webhook 接收入口。"""
    tid = generate_trace_id()
    set_trace_id(tid)

    raw_body = await request.body()
    if Config.security.MAX_WEBHOOK_BODY_BYTES and len(raw_body) > Config.security.MAX_WEBHOOK_BODY_BYTES:
        return JSONResponse(status_code=413, content={"success": False, "error": "Payload too large"})

    client_ip = get_client_ip(request)
    headers = dict(request.headers)
    raw_body_str = raw_body.decode("utf-8", errors="replace")

    event_id = await quick_receive_webhook(
        session=session,
        source=source,
        raw_headers=headers,
        raw_body=raw_body_str,
    )
    await session.commit()
    set_trace_id(generate_trace_id(event_id=event_id))

    await process_webhook_task.kiq(event_id=event_id, client_ip=client_ip or "")

    return {"success": True, "message": "Webhook received and queued for processing", "event_id": event_id}


# ── 查询路由 ───────────────────────────────────────────────────────────────────


@webhook_router.get("/api/webhooks", response_model=WebhookListResponse)
async def get_webhooks_endpoint(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    cursor: int | None = Query(None, alias="cursor_id"),
    importance: str = Query(""),
    source: str = Query(""),
    session: AsyncSession = Depends(get_db_session),
):
    """获取所有 webhook 事件的摘要列表。"""
    items, has_more, next_cursor = await list_webhook_summaries(
        session, cursor_id=cursor, importance=importance, source=source, page_size=page_size
    )
    return {
        "success": True,
        "data": items,
        "pagination": {"next_cursor": next_cursor, "has_more": has_more, "page_size": page_size},
    }


@webhook_router.get("/api/webhooks/{webhook_id}", response_model=WebhookDetailResponse)
async def get_webhook_detail_endpoint(webhook_id: int, session: AsyncSession = Depends(get_db_session)):
    """获取单个 webhook 事件的详细信息。"""
    event = await session.get(WebhookEvent, webhook_id)
    if not event:
        return JSONResponse(status_code=404, content={"success": False, "error": "Webhook not found"})

    return {"success": True, "data": event}

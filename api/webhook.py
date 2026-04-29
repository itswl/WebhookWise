"""
api/webhook.py
=====================
Webhook 接收 + 健康检查 + Dashboard + Webhooks API 路由。
"""

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from core.auth import verify_api_key
from core.config import Config
from core.logger import logger
from core.redis_client import get_redis
from core.trace import generate_trace_id, set_trace_id
from core.webhook_security import check_rate_limit_dep, verify_webhook_auth_dep
from crud.webhook import get_client_ip, quick_receive_webhook
from db.session import get_db_session, test_db_connection
from models import WebhookEvent

webhook_router = APIRouter()


# ── 健康检查 & Dashboard ────────────────────────────────────────────────────────


@webhook_router.get("/health")
async def health_check():
    db_ok = await test_db_connection()
    status = "healthy" if db_ok else "unhealthy"
    code = 200 if db_ok else 503
    return JSONResponse(
        content={"success": True, "data": {"status": status, "database": "ok" if db_ok else "failed"}}, status_code=code
    )


@webhook_router.get("/")
async def dashboard():
    return FileResponse("templates/dashboard.html")


# ── Webhooks API ────────────────────────────────────────────────────────────────


@webhook_router.get("/api/webhooks", dependencies=[Depends(verify_api_key)])
async def list_webhooks(
    page: int = Query(1, ge=1, description="Deprecated: 保留向后兼容，实际不影响查询。请使用 cursor_id 分页。"),
    page_size: int = Query(20, ge=1, le=500),
    fields: str = Query("summary"),
    importance: str = Query(""),
    source: str = Query(""),
    cursor_id: int | None = Query(None),
    session: AsyncSession = Depends(get_db_session),
):
    # 向后兼容：客户端传了 page>1 但没传 cursor_id 时给出提示
    if page > 1 and cursor_id is None:
        return JSONResponse(
            status_code=200,
            content={
                "success": True,
                "data": [],
                "status": 200,
                "pagination": {
                    "page_size": page_size,
                    "next_cursor": None,
                    "has_more": False,
                    "hint": "OFFSET pagination is removed. Please use cursor_id for paging. "
                    "Start without cursor_id to get the first page, then use next_cursor.",
                },
            },
        )

    try:
        query = select(WebhookEvent)

        if cursor_id is not None:
            query = query.where(WebhookEvent.id < cursor_id)
        if importance:
            query = query.filter(WebhookEvent.importance == importance)
        if source:
            query = query.filter(WebhookEvent.source == source)

        query = query.order_by(WebhookEvent.id.desc()).limit(page_size + 1)

        normalized_fields = (fields or "summary").lower().strip()
        return_full = normalized_fields in {"full", "all"}

        if not return_full:
            query = query.options(
                defer(WebhookEvent.raw_payload),
                defer(WebhookEvent.headers),
                defer(WebhookEvent.parsed_data),
                defer(WebhookEvent.ai_analysis),
            )

        result = await session.execute(query)
        events = list(result.scalars().all())

        # page_size+1 策略：多取一条判断 has_more
        has_more = len(events) > page_size
        if has_more:
            events = events[:page_size]

        items = []
        for event in events:
            d = event.to_dict() if return_full else event.to_summary_dict()
            d["prev_alert_id"] = event.prev_alert_id
            beyond_window = bool(event.beyond_window)
            d["beyond_time_window"] = beyond_window
            d["is_within_window"] = bool(event.is_duplicate and not beyond_window) if event.is_duplicate else False
            items.append(d)

        next_cursor = events[-1].id if has_more and events else None

        return {
            "success": True,
            "data": items,
            "status": 200,
            "pagination": {
                "page_size": page_size,
                "next_cursor": next_cursor,
                "has_more": has_more,
            },
        }
    except Exception as e:
        logger.error(f"获取 webhook 列表失败: {e!s}", exc_info=True)
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@webhook_router.get("/api/webhooks/cursor", dependencies=[Depends(verify_api_key)])
async def list_webhooks_cursor(
    limit: int = Query(200, ge=1, le=500),
    fields: str = Query("summary"),
    importance: str = Query(""),
    source: str = Query(""),
    cursor_id: int | None = Query(None),
    session: AsyncSession = Depends(get_db_session),
):
    try:
        query = select(WebhookEvent).order_by(WebhookEvent.timestamp.desc(), WebhookEvent.id.desc())

        if importance:
            query = query.filter(WebhookEvent.importance == importance)
        if source:
            query = query.filter(WebhookEvent.source == source)
        if cursor_id is not None:
            query = query.filter(WebhookEvent.id < cursor_id)

        normalized_fields = (fields or "summary").lower().strip()
        return_full = normalized_fields in {"full", "all"}

        if not return_full:
            query = query.options(
                defer(WebhookEvent.raw_payload),
                defer(WebhookEvent.headers),
                defer(WebhookEvent.parsed_data),
                defer(WebhookEvent.ai_analysis),
            )

        result = await session.execute(query.limit(limit))
        events = result.scalars().all()

        items = []
        for event in events:
            d = event.to_dict() if return_full else event.to_summary_dict()
            d["prev_alert_id"] = event.prev_alert_id
            beyond_window = bool(event.beyond_window)
            d["beyond_time_window"] = beyond_window
            d["is_within_window"] = bool(event.is_duplicate and not beyond_window) if event.is_duplicate else False
            items.append(d)

        has_more = len(events) == limit
        next_cursor = events[-1].id if has_more else None

        return {
            "success": True,
            "data": items,
            "status": 200,
            "cursor": {
                "limit": limit,
                "next_cursor": next_cursor,
                "has_more": has_more,
            },
        }
    except Exception as e:
        logger.error(f"获取 webhook 游标列表失败: {e!s}", exc_info=True)
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@webhook_router.get("/api/webhooks/{webhook_id}", dependencies=[Depends(verify_api_key)])
async def get_webhook_detail(webhook_id: int, session: AsyncSession = Depends(get_db_session)):
    result = await session.execute(select(WebhookEvent).filter_by(id=webhook_id))
    event = result.scalars().first()
    if not event:
        return JSONResponse({"success": False, "error": "Webhook not found"}, status_code=404)
    return {"success": True, "data": event.to_dict()}


# ── Webhook 接收 ───────────────────────────────────────────────────────────────


@webhook_router.post(
    "/webhook",
    dependencies=[Depends(check_rate_limit_dep), Depends(verify_webhook_auth_dep)],
)
async def receive_webhook(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    # 入口处设置 trace_id（此时还没有 event_id，用 UUID 短码）
    tid = generate_trace_id()
    set_trace_id(tid)

    raw_body = await request.body()
    if Config.security.MAX_WEBHOOK_BODY_BYTES and len(raw_body) > Config.security.MAX_WEBHOOK_BODY_BYTES:
        return JSONResponse(status_code=413, content={"success": False, "error": "Payload too large"})
    content_type = request.headers.get("content-type", "").lower()
    parsed_data = None
    if raw_body and "application/json" in content_type:
        try:
            parsed_data = await request.json()
        except Exception:
            return JSONResponse(status_code=400, content={"success": False, "error": "Invalid JSON"})

    client_ip = get_client_ip(request)
    headers = dict(request.headers)
    raw_body_str = raw_body.decode("utf-8", errors="replace")

    # ★ 同步入库：202 之前持久化原始数据
    event = await quick_receive_webhook(
        session=session,
        source=headers.get("x-webhook-source", "unknown"),
        raw_headers=headers,
        raw_body=raw_body_str,
        parsed_data=parsed_data,
    )
    # 显式提交：Worker 使用独立 session，需要在此确保数据已落盘
    await session.commit()

    # 更新为 event_id 格式的 trace_id
    set_trace_id(generate_trace_id(event_id=event.id))

    # 通过 Redis Stream 投递给 Worker 异步处理
    redis = get_redis()
    await redis.xadd(Config.server.WEBHOOK_MQ_QUEUE, {"event_id": str(event.id), "client_ip": client_ip or ""})
    return JSONResponse(
        status_code=202,
        content={"success": True, "message": "Webhook received and queued for processing", "event_id": event.id},
    )


@webhook_router.post(
    "/webhook/{source}",
    dependencies=[Depends(check_rate_limit_dep), Depends(verify_webhook_auth_dep)],
)
async def receive_webhook_with_source(
    source: str,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    # 入口处设置 trace_id
    tid = generate_trace_id()
    set_trace_id(tid)

    raw_body = await request.body()
    if Config.security.MAX_WEBHOOK_BODY_BYTES and len(raw_body) > Config.security.MAX_WEBHOOK_BODY_BYTES:
        return JSONResponse(status_code=413, content={"success": False, "error": "Payload too large"})
    content_type = request.headers.get("content-type", "").lower()
    parsed_data = None
    if raw_body and "application/json" in content_type:
        try:
            parsed_data = await request.json()
        except Exception:
            return JSONResponse(status_code=400, content={"success": False, "error": "Invalid JSON"})

    client_ip = get_client_ip(request)
    headers = dict(request.headers)
    raw_body_str = raw_body.decode("utf-8", errors="replace")

    # ★ 同步入库：202 之前持久化原始数据
    event = await quick_receive_webhook(
        session=session,
        source=source,
        raw_headers=headers,
        raw_body=raw_body_str,
        parsed_data=parsed_data,
    )
    # 显式提交：Worker 使用独立 session，需要在此确保数据已落盘
    await session.commit()

    # 更新为 event_id 格式的 trace_id
    set_trace_id(generate_trace_id(event_id=event.id))

    # 通过 Redis Stream 投递给 Worker 异步处理
    redis = get_redis()
    await redis.xadd(Config.server.WEBHOOK_MQ_QUEUE, {"event_id": str(event.id), "client_ip": client_ip or ""})
    return JSONResponse(
        status_code=202,
        content={"success": True, "message": "Webhook received and queued for processing", "event_id": event.id},
    )

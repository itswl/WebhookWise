"""
api/webhook.py
=====================
Webhook 接收 + 健康检查 + Dashboard + Webhooks API 路由。
"""

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth import verify_api_key
from core.config import Config
from core.logger import logger
from core.redis_client import get_redis
from core.trace import generate_trace_id, set_trace_id
from core.webhook_security import check_rate_limit_dep, verify_webhook_auth_dep
from crud.webhook import get_client_ip, list_webhook_summaries, list_webhook_summaries_cursor, quick_receive_webhook
from db.session import get_db_session, test_db_connection
from models import WebhookEvent
from schemas.webhook import HealthResponse, WebhookDetailResponse, WebhookListResponse, WebhookReceiveResponse

webhook_router = APIRouter()

def _apply_duplicate_fields(d: dict) -> dict:
    is_dup = bool(d.get("is_duplicate"))
    beyond_window = bool(d.get("beyond_window"))
    d["is_duplicate"] = is_dup
    d["beyond_window"] = beyond_window
    d["beyond_time_window"] = beyond_window
    d["is_within_window"] = bool(is_dup and not beyond_window)
    if is_dup:
        d["duplicate_type"] = "beyond_window" if beyond_window else "within_window"
    else:
        d["duplicate_type"] = "new"
    return d


# ── 健康检查 & Dashboard ────────────────────────────────────────────────────────


@webhook_router.get("/health", response_model=HealthResponse)
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


@webhook_router.get("/api/webhooks", dependencies=[Depends(verify_api_key)], response_model=WebhookListResponse)
async def list_webhooks(
    page_size: int = Query(20, ge=1, le=500),
    fields: str = Query("summary"),
    importance: str = Query(""),
    source: str = Query(""),
    cursor_id: int | None = Query(None),
    session: AsyncSession = Depends(get_db_session),
):
    try:
        normalized_fields = (fields or "summary").lower().strip()
        return_full = normalized_fields in {"full", "all"}

        if return_full:
            # 完整模式：仍走 ORM 实例路径
            query = select(WebhookEvent)
            if cursor_id is not None:
                query = query.where(WebhookEvent.id < cursor_id)
            if importance:
                query = query.filter(WebhookEvent.importance == importance)
            if source:
                query = query.filter(WebhookEvent.source == source)
            query = query.order_by(WebhookEvent.id.desc()).limit(page_size + 1)

            result = await session.execute(query)
            events = list(result.scalars().all())

            has_more = len(events) > page_size
            if has_more:
                events = events[:page_size]

            items = []
            for event in events:
                d = event.to_dict()
                d["prev_alert_id"] = event.prev_alert_id
                items.append(_apply_duplicate_fields(d))

            next_cursor = events[-1].id if has_more and events else None
        else:
            # 摘要模式：投影查询，避免 ORM 实例化
            items, has_more, next_cursor = await list_webhook_summaries(
                session,
                cursor_id=cursor_id,
                importance=importance,
                source=source,
                page_size=page_size,
            )

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


@webhook_router.get("/api/webhooks/cursor", dependencies=[Depends(verify_api_key)], response_model=WebhookListResponse)
async def list_webhooks_cursor(
    limit: int = Query(200, ge=1, le=500),
    fields: str = Query("summary"),
    importance: str = Query(""),
    source: str = Query(""),
    cursor_id: int | None = Query(None),
    session: AsyncSession = Depends(get_db_session),
):
    try:
        normalized_fields = (fields or "summary").lower().strip()
        return_full = normalized_fields in {"full", "all"}

        if return_full:
            # 完整模式：仍走 ORM 实例路径
            query = select(WebhookEvent).order_by(WebhookEvent.timestamp.desc(), WebhookEvent.id.desc())
            if importance:
                query = query.filter(WebhookEvent.importance == importance)
            if source:
                query = query.filter(WebhookEvent.source == source)
            if cursor_id is not None:
                query = query.filter(WebhookEvent.id < cursor_id)

            result = await session.execute(query.limit(limit))
            events = result.scalars().all()

            items = []
            for event in events:
                d = event.to_dict()
                d["prev_alert_id"] = event.prev_alert_id
                items.append(_apply_duplicate_fields(d))

            has_more = len(events) == limit
            next_cursor = events[-1].id if has_more else None
        else:
            # 摘要模式：投影查询，避免 ORM 实例化
            items, has_more, next_cursor = await list_webhook_summaries_cursor(
                session,
                cursor_id=cursor_id,
                importance=importance,
                source=source,
                limit=limit,
            )

        return {
            "success": True,
            "data": items,
            "status": 200,
            "pagination": {
                "limit": limit,
                "next_cursor": next_cursor,
                "has_more": has_more,
            },
        }
    except Exception as e:
        logger.error(f"获取 webhook 游标列表失败: {e!s}", exc_info=True)
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@webhook_router.get(
    "/api/webhooks/{webhook_id}", dependencies=[Depends(verify_api_key)], response_model=WebhookDetailResponse
)
async def get_webhook_detail(webhook_id: int, session: AsyncSession = Depends(get_db_session)):
    result = await session.execute(select(WebhookEvent).filter_by(id=webhook_id))
    event = result.scalars().first()
    if not event:
        return JSONResponse({"success": False, "error": "Webhook not found"}, status_code=404)
    d = event.to_dict()
    d["prev_alert_id"] = event.prev_alert_id
    return {"success": True, "data": _apply_duplicate_fields(d)}


# ── Webhook 接收 ───────────────────────────────────────────────────────────────


@webhook_router.post(
    "/webhook",
    dependencies=[Depends(check_rate_limit_dep), Depends(verify_webhook_auth_dep)],
    response_model=WebhookReceiveResponse,
    status_code=202,
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

    client_ip = get_client_ip(request)
    headers = dict(request.headers)
    raw_body_str = raw_body.decode("utf-8", errors="replace")

    # ★ 网关零解析：仅持久化原始 bytes，parsed_data 由 Worker 延迟解析
    event_id = await quick_receive_webhook(
        session=session,
        source=headers.get("x-webhook-source", "unknown"),
        raw_headers=headers,
        raw_body=raw_body_str,
        parsed_data=None,
    )
    # 显式提交：Worker 使用独立 session，需要在此确保数据已落盘
    await session.commit()

    # 更新为 event_id 格式的 trace_id
    set_trace_id(generate_trace_id(event_id=event_id))

    # 通过 Redis Stream 投递给 Worker 异步处理
    redis = get_redis()
    # CRITICAL: xadd 必须在 session.commit() 之后执行，
    # 否则 Worker 可能读到未提交的脏数据。禁止调换顺序。
    await redis.xadd(
        Config.server.WEBHOOK_MQ_QUEUE,
        {"event_id": str(event_id), "client_ip": client_ip or ""},
        maxlen=Config.server.WEBHOOK_MQ_STREAM_MAXLEN,
        approximate=True,
    )
    return JSONResponse(
        status_code=202,
        content={"success": True, "message": "Webhook received and queued for processing", "event_id": event_id},
    )


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
    # 入口处设置 trace_id
    tid = generate_trace_id()
    set_trace_id(tid)

    raw_body = await request.body()
    if Config.security.MAX_WEBHOOK_BODY_BYTES and len(raw_body) > Config.security.MAX_WEBHOOK_BODY_BYTES:
        return JSONResponse(status_code=413, content={"success": False, "error": "Payload too large"})

    client_ip = get_client_ip(request)
    headers = dict(request.headers)
    raw_body_str = raw_body.decode("utf-8", errors="replace")

    # ★ 网关零解析：仅持久化原始 bytes，parsed_data 由 Worker 延迟解析
    event_id = await quick_receive_webhook(
        session=session,
        source=source,
        raw_headers=headers,
        raw_body=raw_body_str,
        parsed_data=None,
    )
    # 显式提交：Worker 使用独立 session，需要在此确保数据已落盘
    await session.commit()

    # 更新为 event_id 格式的 trace_id
    set_trace_id(generate_trace_id(event_id=event_id))

    # 通过 Redis Stream 投递给 Worker 异步处理
    redis = get_redis()
    # CRITICAL: xadd 必须在 session.commit() 之后执行，
    # 否则 Worker 可能读到未提交的脏数据。禁止调换顺序。
    await redis.xadd(
        Config.server.WEBHOOK_MQ_QUEUE,
        {"event_id": str(event_id), "client_ip": client_ip or ""},
        maxlen=Config.server.WEBHOOK_MQ_STREAM_MAXLEN,
        approximate=True,
    )
    return JSONResponse(
        status_code=202,
        content={"success": True, "message": "Webhook received and queued for processing", "event_id": event_id},
    )

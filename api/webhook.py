"""
api/webhook.py
=====================
Webhook 接收 + 健康检查 + Dashboard + Webhooks API 路由。
"""

import time
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth import verify_api_key
from core.log_context import clear_log_context, set_log_context
from core.logger import get_logger
from core.observability.metrics import (
    QUEUE_OPERATION_DURATION_SECONDS,
    QUEUE_OPERATIONS_TOTAL,
    WEBHOOK_INGRESS_PAYLOAD_BYTES,
    WEBHOOK_RECEIVED_TOTAL,
    sanitize_source,
)
from core.observability.tracing import build_traceparent, get_or_generate_trace_id, set_fallback_trace_id
from core.redis_client import redis_ping
from core.request_ip import get_client_ip
from core.sensitive_data import redact_event_dict
from core.webhook_security import check_rate_limit_dep, verify_webhook_auth_dep
from db.engine import test_db_connection
from db.session import get_db_session
from models import WebhookEvent
from schemas import HealthResponse, WebhookListResponse, WebhookReceiveResponse
from schemas.webhook import webhook_event_to_full_dict
from services.operations.tasks import process_webhook_task
from services.webhooks.ingress_backpressure import check_ingress_backpressure
from services.webhooks.policies import IngressPolicy
from services.webhooks.query_service import list_webhook_summaries

logger = get_logger("api.webhook")

webhook_router = APIRouter()

JSONDict = dict[str, Any]
MAX_SOURCE_LENGTH = 100


def _normalize_source_hint(value: str | None) -> str:
    source = str(value or "").strip() or "unknown"
    if len(source) > MAX_SOURCE_LENGTH:
        raise HTTPException(status_code=400, detail=f"source must be at most {MAX_SOURCE_LENGTH} characters")
    return source


def _payload_too_large_response(
    raw_body: bytes, source_hint: str, *, policy: IngressPolicy | None = None
) -> JSONResponse | None:
    policy = policy or IngressPolicy.from_config()
    if policy.max_body_bytes and len(raw_body) > policy.max_body_bytes:
        src = sanitize_source(source_hint)
        WEBHOOK_RECEIVED_TOTAL.labels(source=src, status="rejected_size").inc()
        WEBHOOK_INGRESS_PAYLOAD_BYTES.labels(source=src, outcome="rejected_size").observe(len(raw_body))
        return JSONResponse(status_code=413, content={"success": False, "error": "Payload too large"})
    return None


async def _receive_and_enqueue_webhook(
    *,
    request: Request,
    source_hint: str,
    request_id: str,
) -> JSONDict | JSONResponse:
    try:
        client_ip = get_client_ip(request)
    except Exception:
        client_ip = "unknown"
    state = getattr(request, "state", None)
    raw_body = (getattr(state, "raw_body", None) if state is not None else None) or await request.body()
    content_length = request.headers.get("content-length", "")
    content_type = request.headers.get("content-type", "")
    method = str(getattr(request, "method", ""))
    path = str(getattr(getattr(request, "url", None), "path", ""))
    logger.info(
        "[Webhook] 收到告警 request_id=%s source=%s method=%s path=%s ip=%s content_length=%s body_size=%d content_type=%s",
        request_id,
        source_hint,
        method,
        path,
        client_ip,
        content_length,
        len(raw_body),
        content_type,
    )
    if too_large_response := _payload_too_large_response(raw_body, source_hint):
        logger.warning(
            "[Webhook] 拒绝超大 payload request_id=%s source=%s ip=%s body_size=%d",
            request_id,
            source_hint,
            client_ip,
            len(raw_body),
        )
        return too_large_response

    backpressure = await check_ingress_backpressure(source_hint=source_hint, raw_body=raw_body)
    if backpressure.suppressed:
        src = sanitize_source(source_hint)
        WEBHOOK_RECEIVED_TOTAL.labels(source=src, status="ingress_suppressed").inc()
        WEBHOOK_INGRESS_PAYLOAD_BYTES.labels(source=src, outcome="ingress_suppressed").observe(len(raw_body))
        logger.warning(
            "[Webhook] ingress 背压抑制 request_id=%s source=%s ip=%s body_size=%d count=%s threshold=%s key=%s reason=%s",
            request_id,
            source_hint,
            client_ip,
            len(raw_body),
            backpressure.count,
            backpressure.threshold,
            backpressure.key,
            backpressure.reason,
        )
        return {
            "success": True,
            "message": "Webhook suppressed by ingress backpressure",
            "event_id": None,
            "request_id": request_id,
        }

    headers = dict(request.headers)
    raw_body_str = raw_body.decode("utf-8", errors="replace")
    received_at = datetime.now().astimezone().isoformat(timespec="seconds")

    task_kwargs: dict[str, Any] = {
        "source_name": source_hint,
        "raw_headers": headers,
        "raw_body": raw_body_str,
        "client_ip": client_ip or "",
        "request_id": request_id,
        "received_at": received_at,
        "traceparent": headers.get("traceparent") or build_traceparent(request_id),
    }
    enqueue_started = time.perf_counter()
    enqueue_status = "success"
    try:
        await process_webhook_task.kiq(**task_kwargs)
    except Exception:
        enqueue_status = "error"
        WEBHOOK_INGRESS_PAYLOAD_BYTES.labels(source=sanitize_source(source_hint), outcome="enqueue_failed").observe(
            len(raw_body)
        )
        logger.exception(
            "[Webhook] 告警入队失败 request_id=%s source=%s ip=%s body_size=%d",
            request_id,
            source_hint,
            client_ip,
            len(raw_body),
        )
        raise
    finally:
        QUEUE_OPERATIONS_TOTAL.labels("webhook_process_task", "enqueue", enqueue_status).inc()
        QUEUE_OPERATION_DURATION_SECONDS.labels("webhook_process_task", "enqueue", enqueue_status).observe(
            time.perf_counter() - enqueue_started
        )
    logger.info(
        "[Webhook] 告警已入队 request_id=%s source=%s ip=%s body_size=%d received_at=%s",
        request_id,
        source_hint,
        client_ip,
        len(raw_body),
        received_at,
    )
    WEBHOOK_INGRESS_PAYLOAD_BYTES.labels(source=sanitize_source(source_hint), outcome="queued").observe(len(raw_body))

    return {
        "success": True,
        "message": "Webhook received and queued for processing",
        "event_id": None,
        "request_id": request_id,
    }


# ── 基础路由 ───────────────────────────────────────────────────────────────────


@webhook_router.get("/live", response_model=HealthResponse)
async def liveness_check() -> JSONResponse:
    """进程存活检查，不触碰外部依赖。"""
    return JSONResponse(content={"success": True, "data": {"status": "alive"}}, status_code=200)


@webhook_router.get("/ready", response_model=HealthResponse)
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
) -> JSONDict | JSONResponse:
    """通用 Webhook 接收入口。"""
    request_id = get_or_generate_trace_id()
    set_fallback_trace_id(request_id)
    source_hint = _normalize_source_hint(source or request.headers.get("x-webhook-source"))
    clear_log_context()
    set_log_context(request_id=request_id, source=source_hint)

    return await _receive_and_enqueue_webhook(
        request=request,
        source_hint=source_hint,
        request_id=request_id,
    )


@webhook_router.post(
    "/webhook/{source}",
    dependencies=[Depends(check_rate_limit_dep), Depends(verify_webhook_auth_dep)],
    response_model=WebhookReceiveResponse,
    status_code=202,
)
async def receive_webhook_with_source(
    request: Request,
    source: str = Path(..., max_length=MAX_SOURCE_LENGTH),
) -> JSONDict | JSONResponse:
    """带来源标识的 Webhook 接收入口。"""
    request_id = get_or_generate_trace_id()
    set_fallback_trace_id(request_id)
    source_hint = _normalize_source_hint(source)
    clear_log_context()
    set_log_context(request_id=request_id, source=source_hint)

    return await _receive_and_enqueue_webhook(
        request=request,
        source_hint=source_hint,
        request_id=request_id,
    )


# ── 查询路由 ───────────────────────────────────────────────────────────────────


@webhook_router.get("/api/webhooks", dependencies=[Depends(verify_api_key)], response_model=WebhookListResponse)
async def get_webhooks_endpoint(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=500),
    cursor: int | None = Query(None),
    importance: str = Query(""),
    source: str = Query(""),
    session: AsyncSession = Depends(get_db_session),
) -> JSONDict:
    """获取所有 webhook 事件的摘要列表。"""
    items, has_more, next_cursor = await list_webhook_summaries(
        session, cursor=cursor, importance=importance, source=source, page=page, page_size=page_size
    )
    return {
        "success": True,
        "data": items,
        "pagination": {"next_cursor": next_cursor, "has_more": has_more, "page_size": page_size},
    }


@webhook_router.get(
    "/api/webhooks/by-request/{request_id}",
    dependencies=[Depends(verify_api_key)],
    response_model=None,
)
async def get_webhook_by_request_id_endpoint(
    request_id: str = Path(..., min_length=1, max_length=64),
    session: AsyncSession = Depends(get_db_session),
) -> JSONDict | JSONResponse:
    """按异步接收 request_id 查询最终持久化事件。"""
    stmt = select(WebhookEvent).where(WebhookEvent.request_id == request_id)
    event = (await session.execute(stmt)).scalar_one_or_none()
    if not event:
        return JSONResponse(status_code=404, content={"success": False, "error": "Webhook not found"})

    return {"success": True, "data": redact_event_dict(webhook_event_to_full_dict(event))}


@webhook_router.get("/api/webhooks/{webhook_id}", dependencies=[Depends(verify_api_key)], response_model=None)
async def get_webhook_detail_endpoint(
    webhook_id: int, session: AsyncSession = Depends(get_db_session)
) -> JSONDict | JSONResponse:
    """获取单个 webhook 事件的详细信息。"""
    event = await session.get(WebhookEvent, webhook_id)
    if not event:
        return JSONResponse(status_code=404, content={"success": False, "error": "Webhook not found"})

    return {"success": True, "data": redact_event_dict(webhook_event_to_full_dict(event))}

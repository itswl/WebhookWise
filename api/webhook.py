"""
api/webhook.py
=====================
Webhook 接收 + 健康检查 + Dashboard + Webhooks API 路由。
"""

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from adapters.ecosystem_adapters import normalize_webhook_event
from core.auth import verify_api_key
from core.datetime_utils import utcnow
from core.log_context import clear_log_context, set_log_context
from core.logger import get_logger
from core.observability.attributes import REQUEST_ID, WEBHOOK_SOURCE
from core.observability.metrics import (
    QUEUE_OPERATION_DURATION_SECONDS,
    QUEUE_OPERATIONS_TOTAL,
    WEBHOOK_INGRESS_PAYLOAD_BYTES,
    WEBHOOK_INGRESS_REQUEST_DURATION_SECONDS,
    WEBHOOK_INGRESS_REQUESTS_TOTAL,
    WEBHOOK_RECEIVED_TOTAL,
    sanitize_source,
)
from core.observability.tracing import (
    add_span_event_to,
    generate_trace_id,
    get_current_trace_id,
    inject_trace_headers,
    otel_span,
    reset_fallback_trace_id,
    set_fallback_trace_id,
    set_span_ok,
)
from core.redis_client import redis_ping
from core.request_ip import get_client_ip
from core.sensitive_data import redact_event_dict
from core.webhook_security import check_rate_limit_dep, verify_webhook_auth_dep
from db.engine import test_db_connection
from db.session import get_db_session
from models import WebhookEvent
from schemas.webhook import HealthResponse, WebhookListResponse, WebhookReceiveResponse, webhook_event_to_full_dict
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
    received_at = utcnow().isoformat(timespec="seconds")

    trace_headers = inject_trace_headers(
        {},
        request_id=request_id,
        fallback_trace_id=get_current_trace_id() or getattr(state, "trace_id", "") or generate_trace_id(),
    )
    task_kwargs: dict[str, Any] = {
        "source_name": source_hint,
        "raw_headers": headers,
        "raw_body": raw_body_str,
        "client_ip": client_ip or "",
        "request_id": request_id,
        "received_at": received_at,
        "traceparent": trace_headers.get("traceparent") or headers.get("traceparent"),
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
@webhook_router.post(
    "/webhook/{source}",
    dependencies=[Depends(check_rate_limit_dep), Depends(verify_webhook_auth_dep)],
    response_model=WebhookReceiveResponse,
    status_code=202,
)
async def receive_webhook(
    request: Request,
    source: str | None = None,
) -> JSONDict | JSONResponse:
    """Webhook 接收入口（支持 /webhook 和 /webhook/{source}）。"""
    ingress_started = time.perf_counter()
    ingress_outcome = "accepted"
    request_id = request.headers.get("x-request-id") or getattr(request.state, "request_id", "") or generate_trace_id()
    token = set_fallback_trace_id(
        get_current_trace_id() or getattr(request.state, "trace_id", "") or generate_trace_id()
    )
    path_source = request.path_params.get("source")
    source_hint = _normalize_source_hint(path_source or source or request.headers.get("x-webhook-source"))
    metric_source = sanitize_source(source_hint)
    clear_log_context()
    set_log_context(request_id=request_id, webhook_source=source_hint)
    try:
        with otel_span(
            "webhook.ingress",
            {
                REQUEST_ID: request_id,
                WEBHOOK_SOURCE: source_hint,
                "http.request.method": str(request.method),
                "url.path": str(request.url.path),
            },
        ) as ingress_span:
            result = await _receive_and_enqueue_webhook(request=request, source_hint=source_hint, request_id=request_id)
            if isinstance(result, JSONResponse):
                ingress_outcome = "rejected"
            elif result.get("event_id") is None and "suppressed" in str(result.get("message", "")).lower():
                ingress_outcome = "suppressed"
            else:
                ingress_outcome = "queued"
            if ingress_span is not None:
                ingress_span.set_attribute("webhook.outcome", ingress_outcome)
                add_span_event_to(
                    ingress_span,
                    "webhook.ingress.completed",
                    {"webhook.outcome": ingress_outcome},
                )
                set_span_ok(ingress_span)
            return result
    except Exception:
        ingress_outcome = "error"
        raise
    finally:
        WEBHOOK_INGRESS_REQUESTS_TOTAL.labels(metric_source, ingress_outcome).inc()
        WEBHOOK_INGRESS_REQUEST_DURATION_SECONDS.labels(metric_source, ingress_outcome).observe(
            time.perf_counter() - ingress_started
        )
        reset_fallback_trace_id(token)


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


async def build_webhook_context(event: WebhookEvent) -> JSONDict:
    from services.webhooks.repository import load_event_payload

    parsed_data, _ = await load_event_payload(event)
    source = event.source
    if (not source or source == "unknown") and isinstance(parsed_data, dict):
        normalized = normalize_webhook_event(parsed_data, None)
        source, parsed_data = normalized.source or source, normalized.data
    return {
        "source": source,
        "parsed_data": parsed_data,
        "timestamp": event.timestamp.isoformat() if event.timestamp else None,
        "client_ip": event.client_ip,
    }

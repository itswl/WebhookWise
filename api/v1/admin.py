"""
Admin and Management API Routes.
Handles system configuration, prompt management, and dead-letter replay.
"""

from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from adapters.registry import registry as adapter_registry
from api import fail_response, internal_error_response, ok_response
from core.app_context import AppContext
from core.auth import verify_admin_write, verify_api_key
from core.config import get_settings
from core.config.manager import get_config_version, get_reloadable_sections
from core.config.manager import reload_config as _reload_config
from core.datetime_utils import utc_isoformat
from core.logger import get_logger
from core.redis_client import redis_ping
from core.redis_health import get_redis_health_snapshot
from core.redis_streams import redis_xinfo_group_lag, redis_xlen, redis_xpending_pending
from db.engine import test_db_connection
from db.session import get_db_session
from models import WebhookEvent
from schemas.admin import (
    DeadLetterListResponse,
    PromptGetResponse,
    PromptReloadResponse,
    ReplayAllResponse,
    ReplayResponse,
)
from services.analysis.ai_analyzer import (
    get_prompt_source,
    load_deep_analysis_prompt_template,
    load_user_prompt_template,
    reload_deep_analysis_prompt_template,
    reload_user_prompt_template,
)
from services.forwarding.outbox import requeue_forward_outbox
from services.operations.tasks import process_webhook_task
from services.webhooks.query_service import (
    count_dead_letters,
    get_dead_letter_detail,
    list_dead_letters,
)
from services.webhooks.repository import (
    count_suppressed_records,
    list_suppressed_records,
    load_event_payload,
)

logger = get_logger("api.v1.admin")

admin_router = APIRouter()
PromptKind = Literal["user", "deep_analysis"]
_ADMIN_RUNTIME_ERRORS = (OSError, RuntimeError, SQLAlchemyError, TimeoutError)


def _normalize_prompt_kind(kind: str) -> PromptKind:
    normalized = kind.strip().lower().replace("-", "_")
    if normalized in ("user", "ai"):
        return "user"
    if normalized in ("deep_analysis", "deep"):
        return "deep_analysis"
    raise ValueError("unsupported prompt kind")


async def _load_prompt_by_kind(kind: PromptKind) -> str:
    if kind == "deep_analysis":
        return await load_deep_analysis_prompt_template()
    return await load_user_prompt_template()


async def _reload_prompt_by_kind(kind: PromptKind) -> str:
    if kind == "deep_analysis":
        return await reload_deep_analysis_prompt_template()
    return await reload_user_prompt_template()


@admin_router.get("/health/deep", dependencies=[Depends(verify_api_key)])
async def deep_health_check(request: Request) -> JSONResponse:
    context = getattr(request.app.state, "app_context", None)
    config = context.config if isinstance(context, AppContext) else get_settings()
    db_ok = await test_db_connection()
    redis_ok = await redis_ping()
    redis_snapshot = get_redis_health_snapshot()
    queue_stream = config.mq.WEBHOOK_MQ_QUEUE
    queue_group = config.mq.WEBHOOK_MQ_CONSUMER_GROUP

    queue_depth: int | None = None
    queue_pending: int | None = None
    queue_lag: int | None = None
    queue_ok = False
    try:
        queue_depth = await redis_xlen(queue_stream)
        queue_pending = await redis_xpending_pending(queue_stream, queue_group)
        queue_lag = await redis_xinfo_group_lag(queue_stream, queue_group)
        queue_ok = True
    except _ADMIN_RUNTIME_ERRORS as e:
        logger.warning("[HealthDeep] 读取队列状态失败: %s", e)

    adapter_status = adapter_registry.status()

    ai_configured = bool(config.ai.OPENAI_API_KEY)
    deep_ok = db_ok and redis_ok and queue_ok
    return ok_response(
        http_status=200 if deep_ok else 503,
        data={
            "status": "ok" if deep_ok else "degraded",
            "database": {"ok": db_ok},
            "redis": {
                "ok": redis_ok,
                "health": {
                    "state": redis_snapshot.state.value,
                    "consecutive_failures": redis_snapshot.consecutive_failures,
                    "last_success_at": redis_snapshot.last_success_at,
                    "last_failure_at": redis_snapshot.last_failure_at,
                    "last_error": redis_snapshot.last_error,
                    "last_operation": redis_snapshot.last_operation,
                },
            },
            "queue": {
                "ok": queue_ok,
                "stream": queue_stream,
                "group": queue_group,
                "depth": queue_depth,
                "pending": queue_pending,
                "lag": queue_lag,
            },
            "adapters": adapter_status,
            "ai": {"enabled": bool(config.ai.ENABLE_AI_ANALYSIS), "configured": ai_configured},
            "openclaw": {
                "enabled": bool(config.openclaw.OPENCLAW_ENABLED),
                "configured": bool(config.openclaw.OPENCLAW_GATEWAY_TOKEN),
            },
        },
    )


@admin_router.post(
    "/prompt/reload",
    response_model=PromptReloadResponse,
    dependencies=[Depends(verify_admin_write)],
)
async def reload_prompt(kind: str = Query("user")) -> JSONResponse:
    try:
        prompt_kind = _normalize_prompt_kind(kind)
        new_template = await _reload_prompt_by_kind(prompt_kind)
        preview = new_template[:200] + ("..." if len(new_template) > 200 else "")
        logger.info("[Admin] Prompt 模板已重新加载 kind=%s length=%s", prompt_kind, len(new_template))
        return ok_response(
            status=200,
            message="Prompt 模板已重新加载",
            kind=prompt_kind,
            source=get_prompt_source(prompt_kind),
            template_length=len(new_template),
            preview=preview,
        )
    except ValueError as e:
        return fail_response(str(e), 400)
    except _ADMIN_RUNTIME_ERRORS as e:
        logger.error("重新加载 prompt 模板失败: %s", e, exc_info=True)
        return internal_error_response()


@admin_router.get("/prompt", response_model=PromptGetResponse, dependencies=[Depends(verify_api_key)])
async def get_prompt(kind: str = Query("user")) -> JSONResponse:
    try:
        prompt_kind = _normalize_prompt_kind(kind)
        template = await _load_prompt_by_kind(prompt_kind)
        return ok_response(status=200, kind=prompt_kind, template=template, source=get_prompt_source(prompt_kind))
    except ValueError as e:
        return fail_response(str(e), 400)
    except _ADMIN_RUNTIME_ERRORS as e:
        logger.error("获取 prompt 模板失败: %s", e, exc_info=True)
        return internal_error_response()


# ── Dead Letter ───────────────────────────────────────────────────────────────


async def _enqueue_dead_letter_event(event: WebhookEvent) -> None:
    headers = {str(k): str(v) for k, v in dict(event.headers or {}).items()}
    _, raw_body = await load_event_payload(event)
    await process_webhook_task.kiq(
        source_name=event.source or "unknown",
        raw_headers=headers,
        raw_body=raw_body,
        client_ip=event.client_ip or "admin-replay",
        request_id=event.request_id,
        received_at=utc_isoformat(event.timestamp),
        ingest_retry_count=max(0, int(event.retry_count or 0)),
    )


@admin_router.get("/admin/dead-letters", response_model=DeadLetterListResponse, dependencies=[Depends(verify_api_key)])
async def get_dead_letters_endpoint(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=500),
    source: str = Query("", description="按来源筛选"),
    status: str = Query("dead_letter", description="处理状态筛选（默认 dead_letter）"),
    search: str = Query("", description="搜索 error_message 内容"),
    time_from: str = Query("", description="起始时间 ISO 格式"),
    time_to: str = Query("", description="结束时间 ISO 格式"),
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    try:
        items = await list_dead_letters(
            session,
            page=page,
            page_size=page_size,
            source=source or None,
            status=status,
            search=search or None,
            time_from=time_from or None,
            time_to=time_to or None,
        )
        total = await count_dead_letters(
            session,
            source=source or None,
            status=status,
            search=search or None,
            time_from=time_from or None,
            time_to=time_to or None,
        )
        return ok_response(
            data=items, http_status=200, pagination={"page": page, "page_size": page_size, "total": total}
        )
    except _ADMIN_RUNTIME_ERRORS as e:
        logger.error("查询 dead_letter 列表失败: %s", e, exc_info=True)
        return internal_error_response()


@admin_router.get(
    "/admin/dead-letters/{event_id}",
    dependencies=[Depends(verify_api_key)],
)
async def get_dead_letter_detail_endpoint(
    event_id: int,
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    try:
        detail = await get_dead_letter_detail(session, event_id)
        if detail is None:
            return fail_response(f"事件 {event_id} 不存在", 404)
        return ok_response(data=detail, http_status=200)
    except _ADMIN_RUNTIME_ERRORS as e:
        logger.error("查询 dead_letter 详情失败: event_id=%s error=%s", event_id, e, exc_info=True)
        return internal_error_response()


@admin_router.post(
    "/admin/outbox/{outbox_id}/retry",
    response_model=None,
    dependencies=[Depends(verify_admin_write)],
)
async def retry_outbox_endpoint(outbox_id: int) -> JSONResponse:
    try:
        if await requeue_forward_outbox(outbox_id):
            logger.info("[Admin] outbox 已重新入队 id=%s", outbox_id)
            return ok_response(http_status=200, message="outbox 已重新入队", data={"outbox_id": outbox_id})
        return fail_response("outbox 不存在或状态不可重试", 400)
    except _ADMIN_RUNTIME_ERRORS as e:
        logger.error("[Admin] outbox 重新入队失败 id=%s error=%s", outbox_id, e, exc_info=True)
        return internal_error_response()


@admin_router.get("/admin/suppressed", dependencies=[Depends(verify_api_key)])
async def list_suppressed_endpoint(
    session: AsyncSession = Depends(get_db_session),
    minutes: int = Query(60, ge=1, le=24 * 60),
    limit: int = Query(100, ge=1, le=500),
) -> JSONResponse:
    try:
        items = await list_suppressed_records(session, since_minutes=minutes, limit=limit)
        total = await count_suppressed_records(session, since_minutes=minutes)
        return ok_response(http_status=200, data={"total": total, "items": items})
    except _ADMIN_RUNTIME_ERRORS as e:
        logger.error("查询 suppressed_records 失败: %s", e, exc_info=True)
        return internal_error_response()


@admin_router.post(
    "/admin/dead-letters/{event_id}/replay",
    response_model=ReplayResponse,
    dependencies=[Depends(verify_admin_write)],
)
async def replay_single_dead_letter(event_id: int, session: AsyncSession = Depends(get_db_session)) -> JSONResponse:
    try:
        event = await session.get(WebhookEvent, event_id)
        if not event or event.processing_status != "dead_letter":
            logger.warning("[Admin] dead_letter 重放失败，状态不匹配或事件不存在 event_id=%s", event_id)
            return fail_response(f"事件 {event_id} 不存在或状态非 dead_letter", 404)
        await _enqueue_dead_letter_event(event)
        logger.info("[Admin] dead_letter 已重放 event_id=%s", event_id)
        return ok_response(http_status=200, message=f"事件 {event_id} 已重放", event_id=event_id)
    except _ADMIN_RUNTIME_ERRORS as e:
        logger.error("重放 dead_letter 失败: event_id=%s, error=%s", event_id, e, exc_info=True)
        return internal_error_response()


@admin_router.post(
    "/admin/dead-letters/replay-all",
    response_model=ReplayAllResponse,
    dependencies=[Depends(verify_admin_write)],
)
async def replay_all_dead_letters(
    batch_size: int = Query(50, ge=1, le=500),
    event_ids: str = Query("", description="指定重放的 event_id 列表（逗号分隔）；为空则重放全部"),
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """批量重放 dead-letter 事件。可指定 event_ids 或多个 ID，不指定则重放全部。"""
    try:
        if event_ids:
            ids = [int(x.strip()) for x in event_ids.split(",") if x.strip()]
            replayed_ids = []
            for eid in ids:
                event = await session.get(WebhookEvent, eid)
                if event and event.processing_status == "dead_letter":
                    replayed_ids.append(eid)
                    await _enqueue_dead_letter_event(event)
            logger.info("[Admin] 指定重放 dead_letter 完成 replayed=%s event_ids=%s", len(replayed_ids), ids)
            return ok_response(
                http_status=200,
                message=f"已重放 {len(replayed_ids)} 条 dead_letter",
                replayed=len(replayed_ids),
                event_ids=replayed_ids,
            )
        items = await list_dead_letters(session, page=1, page_size=batch_size)
        if not items:
            logger.info("[Admin] 批量重放 dead_letter：无待处理记录")
            return ok_response(http_status=200, message="无 dead_letter 需要重放", replayed=0)
        replayed_ids = []
        for item in items:
            eid = item["id"]
            event = await session.get(WebhookEvent, eid)
            if event and event.processing_status == "dead_letter":
                replayed_ids.append(eid)
                await _enqueue_dead_letter_event(event)
        logger.info("[Admin] 批量重放 dead_letter 完成 replayed=%s event_ids=%s", len(replayed_ids), replayed_ids)
        return ok_response(
            http_status=200,
            message=f"已重放 {len(replayed_ids)} 条 dead_letter",
            replayed=len(replayed_ids),
            event_ids=replayed_ids,
        )
    except _ADMIN_RUNTIME_ERRORS as e:
        logger.error("批量重放 dead_letter 失败: %s", e, exc_info=True)
        return internal_error_response()


# ── 配置热加载 ─────────────────────────────────────────────────────────────────


@admin_router.get(
    "/admin/config/version",
    dependencies=[Depends(verify_api_key)],
)
async def get_config_version_endpoint() -> JSONResponse:
    """返回当前配置版本号和各配置段元信息。"""
    version = get_config_version()
    sections = get_reloadable_sections()
    return ok_response(
        http_status=200,
        data={
            "version": version,
            "sections": sections,
        },
    )


@admin_router.post(
    "/admin/config/reload",
    dependencies=[Depends(verify_admin_write)],
)
async def reload_config_endpoint(
    request: Request,
    section: str = "all",
) -> JSONResponse:
    """重新加载配置段（无需重启进程）。从 .env 和环境变量重新读取。"""
    try:
        result = _reload_config(section)
    except _ADMIN_RUNTIME_ERRORS as e:
        logger.error("[Admin] Config reload failed section=%s error=%s", section, e, exc_info=True)
        return internal_error_response()

    if not result.get("success"):
        not_hot = result.get("not_hot_reloadable", False)
        status_code = 400 if not not_hot else 409  # 409 = conflict for not-hot-reloadable
        return fail_response(str(result.get("error", "unknown")), status_code)

    # Update the AppContext config reference so in-process code sees the new values
    context: AppContext | None = getattr(request.app.state, "app_context", None)
    if context is not None:
        from core.config import get_settings as _get_settings

        context.config = _get_settings()
        logger.info("[Admin] AppContext config reference updated section=%s", section)

    return ok_response(
        http_status=200,
        message=f"配置已重新加载: section={section}, settings={result.get('settings_count', 0)}",
        data=result,
    )

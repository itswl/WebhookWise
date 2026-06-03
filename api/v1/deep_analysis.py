import contextlib
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from api import DELIVERY_ERROR_MESSAGE, TARGET_URL_UNAVAILABLE_MESSAGE, internal_error_response
from api.v1.webhook import JSONDict, build_webhook_context
from core.app_context import get_http_client_dependency
from core.auth import verify_admin_write
from core.datetime_utils import utc_isoformat, utcnow
from core.logger import get_logger, mask_url
from core.url_security import UnsafeTargetUrlError, validate_outbound_url
from db.session import get_db_session
from models import DeepAnalysis, WebhookEvent
from schemas.analysis import DeepAnalysisListResponse, deep_analysis_to_dict
from services.analysis.ai_analyzer import analyze_webhook_with_ai
from services.analysis.analysis_queries import get_deep_analyses_for_webhook, get_deep_analysis_list
from services.forwarding.policies import OpenClawTriggerPolicy
from services.notifications.feishu import is_feishu_url
from services.operations import taskiq_retry_scheduler
from services.webhooks.types import (
    MANUAL_RETRY_STARTED_AT,
    AnalysisResult,
    DeepAnalysisStatus,
    ForwardResult,
    JsonObject,
    WebhookData,
    analysis_degraded_reason,
    is_analysis_degraded,
    is_pending_result,
    openclaw_run_id,
    openclaw_session_key,
)

logger = get_logger("api.v1.deep_analysis")

deep_analysis_router = APIRouter()

MAX_PAGE = 500
MANUAL_RETRY_STARTED_AT_KEY = MANUAL_RETRY_STARTED_AT


def _is_supported_deep_analysis_engine(requested: str) -> bool:
    return requested in ("", "auto", "openclaw")


async def _run_openclaw_deep_analysis(
    ctx: JSONDict, headers: dict[str, Any], user_question: str
) -> tuple[AnalysisResult | ForwardResult, str]:
    from services.analysis.openclaw import analyze_with_openclaw

    webhook_data: WebhookData = {
        "source": ctx["source"],
        "headers": headers,
        "parsed_data": ctx["parsed_data"],
    }
    result = await analyze_with_openclaw(webhook_data, user_question)
    if is_analysis_degraded(result):
        logger.warning("[DeepAnalysis] OpenClaw 降级，回退本地 AI: %s", analysis_degraded_reason(result))
        return await analyze_webhook_with_ai(webhook_data), "local (fallback)"
    return result, "openclaw"


async def _notify_completed_deep_analysis(session: AsyncSession, record: DeepAnalysis) -> None:
    from services.operations.deep_analysis_notifications import (
        EVENT_IMPORTANCE_KEY,
        EVENT_IS_DUPLICATE_KEY,
        EVENT_PARSED_DATA_KEY,
        send_deep_analysis_success_notification,
    )

    event = await session.get(WebhookEvent, record.webhook_event_id)
    source = event.source if event else ""
    record_dict: JSONDict = {
        "id": record.id,
        "webhook_event_id": record.webhook_event_id,
        "engine": record.engine,
        "analysis_result": record.analysis_result,
        "duration_seconds": record.duration_seconds,
    }
    if event:
        record_dict[EVENT_IMPORTANCE_KEY] = str(getattr(event, "importance", "") or "")
        record_dict[EVENT_IS_DUPLICATE_KEY] = bool(getattr(event, "is_duplicate", False))
        parsed_data = getattr(event, "parsed_data", None)
        record_dict[EVENT_PARSED_DATA_KEY] = dict(parsed_data or {}) if isinstance(parsed_data, dict) else {}
    await send_deep_analysis_success_notification(record_dict, source)


def _prepare_openclaw_poll_if_pending(record: DeepAnalysis) -> int | None:
    if record.status != DeepAnalysisStatus.PENDING:
        return None
    from services.operations.taskiq_retry_scheduler import compute_openclaw_poll_delay

    delay = compute_openclaw_poll_delay(record.poll_attempts or 0)
    record.next_poll_at = utcnow() + timedelta(seconds=delay)
    return delay


def _reset_deep_analysis_for_background_poll(record: DeepAnalysis, now: datetime) -> None:
    record.status = DeepAnalysisStatus.PENDING
    record.analysis_result = {MANUAL_RETRY_STARTED_AT_KEY: utc_isoformat(now)}
    record.duration_seconds = 0
    record.poll_attempts = 0
    record.last_polled_at = None
    record.next_poll_at = now


@deep_analysis_router.post(
    "/deep-analyze/{webhook_id}",
    response_model=None,
    dependencies=[Depends(verify_admin_write)],
)
async def deep_analyze_webhook(
    webhook_id: int, payload: dict[str, Any] | None = None, session: AsyncSession = Depends(get_db_session)
) -> JSONResponse | JSONDict:
    payload = payload or {}
    logger.info("[DeepAnalysis] 手动分析请求 webhook_id=%s engine=%s", webhook_id, payload.get("engine", "auto"))
    event = await session.get(WebhookEvent, webhook_id)
    if not event:
        logger.warning("[DeepAnalysis] 手动分析失败，事件不存在 webhook_id=%s", webhook_id)
        return JSONResponse(status_code=404, content={"success": False, "error": "Webhook not found"})

    ctx = await build_webhook_context(event)
    requested_engine = str(payload.get("engine", "auto")).strip().lower()
    if not _is_supported_deep_analysis_engine(requested_engine):
        logger.warning("[DeepAnalysis] 不支持的分析引擎 webhook_id=%s engine=%s", webhook_id, requested_engine)
        return JSONResponse(status_code=400, content={"success": False, "error": "Unsupported engine"})
    if not OpenClawTriggerPolicy.from_config().enabled:
        logger.warning("[DeepAnalysis] OpenClaw 未启用 webhook_id=%s", webhook_id)
        return JSONResponse(status_code=503, content={"success": False, "error": "No engine available"})

    try:
        res, engine_name = await _run_openclaw_deep_analysis(
            ctx, event.headers or {}, str(payload.get("user_question", ""))
        )
    except Exception as e:
        logger.error("[DeepAnalysis] 手动分析触发失败 webhook_id=%s error=%s", webhook_id, e, exc_info=True)
        return internal_error_response()

    record = DeepAnalysis(
        webhook_event_id=webhook_id,
        engine=engine_name,
        user_question=payload.get("user_question", ""),
        analysis_result=dict(res),
        status=DeepAnalysisStatus.PENDING if is_pending_result(res) else DeepAnalysisStatus.COMPLETED,
        openclaw_run_id=openclaw_run_id(res),
        openclaw_session_key=openclaw_session_key(res),
    )
    session.add(record)
    await session.flush()
    poll_delay = _prepare_openclaw_poll_if_pending(record)
    analysis_id = int(record.id)
    record_data = deep_analysis_to_dict(record)
    await session.commit()
    if poll_delay is not None:
        await taskiq_retry_scheduler.schedule_openclaw_poll_best_effort(analysis_id, poll_delay)
    logger.info(
        "[DeepAnalysis] 手动分析记录已创建 analysis_id=%s webhook_id=%s status=%s engine=%s poll_delay=%s",
        analysis_id,
        webhook_id,
        record.status,
        engine_name,
        poll_delay,
    )
    return {"success": True, "data": record_data}


@deep_analysis_router.get("/deep-analyses", response_model=DeepAnalysisListResponse)
async def list_all_deep_analyses(
    page: int = Query(1, ge=1, le=MAX_PAGE),
    per_page: int = Query(20, ge=1, le=MAX_PAGE),
    cursor: int | None = Query(None),
    status: str = Query(""),
    engine: str = Query(""),
    session: AsyncSession = Depends(get_db_session),
) -> JSONDict:
    try:
        data = await get_deep_analysis_list(session, page, per_page, cursor, status, engine, MAX_PAGE)
        return {"success": True, "data": data}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@deep_analysis_router.get("/deep-analyses/{webhook_id}")
async def get_deep_analyses(webhook_id: int, session: AsyncSession = Depends(get_db_session)) -> JSONDict:
    records = await get_deep_analyses_for_webhook(session, webhook_id)
    return {"success": True, "data": [deep_analysis_to_dict(record) for record in records]}


@deep_analysis_router.post(
    "/deep-analyses/{analysis_id}/retry",
    response_model=None,
    dependencies=[Depends(verify_admin_write)],
)
async def retry_deep_analysis(
    analysis_id: int, session: AsyncSession = Depends(get_db_session)
) -> JSONResponse | JSONDict:
    """重新拉取或重新发起 OpenClaw 深度分析结果。"""
    logger.info("[DeepAnalysis] 重试请求 analysis_id=%s", analysis_id)
    record = await session.get(DeepAnalysis, analysis_id)
    if not record:
        logger.warning("[DeepAnalysis] 重试失败，记录不存在 analysis_id=%s", analysis_id)
        return JSONResponse(status_code=404, content={"success": False, "error": "分析记录不存在"})

    retryable_statuses = {
        DeepAnalysisStatus.FAILED,
        DeepAnalysisStatus.COMPLETED,
        DeepAnalysisStatus.PENDING,
        DeepAnalysisStatus.TIMEOUT,
        DeepAnalysisStatus.DEGRADED,
        DeepAnalysisStatus.ERROR,
    }
    if record.status not in retryable_statuses:
        logger.warning("[DeepAnalysis] 重试失败，状态不可重试 analysis_id=%s status=%s", analysis_id, record.status)
        return JSONResponse(
            status_code=400,
            content={"success": False, "error": f"当前状态不可重试: {record.status}"},
        )

    if not record.openclaw_session_key:
        event = await session.get(WebhookEvent, record.webhook_event_id)
        if not event:
            logger.warning(
                "[DeepAnalysis] 重试失败，关联 webhook 不存在 analysis_id=%s webhook_id=%s",
                analysis_id,
                record.webhook_event_id,
            )
            return JSONResponse(status_code=404, content={"success": False, "error": "关联的 webhook 事件不存在"})

        ctx = await build_webhook_context(event)
        new_result, engine_name = await _run_openclaw_deep_analysis(
            ctx, event.headers or {}, record.user_question or ""
        )
        if is_pending_result(new_result):
            now = utcnow()
            record.status = DeepAnalysisStatus.PENDING
            record.analysis_result = {**new_result, MANUAL_RETRY_STARTED_AT_KEY: utc_isoformat(now)}
            record.openclaw_run_id = openclaw_run_id(new_result)
            record.openclaw_session_key = openclaw_session_key(new_result)
            record.duration_seconds = 0
            record.poll_attempts = 0
            record.last_polled_at = None
            await session.flush()
            poll_delay = _prepare_openclaw_poll_if_pending(record)
            await session.commit()
            if poll_delay is not None:
                await taskiq_retry_scheduler.schedule_openclaw_poll_best_effort(record.id, poll_delay)
            logger.info("[DeepAnalysis] 已重新发起后台分析 analysis_id=%s poll_delay=%s", record.id, poll_delay)
            return {"success": True, "message": "已重新发起分析任务，请等待结果"}

        record.status = DeepAnalysisStatus.COMPLETED
        record.engine = engine_name
        record.analysis_result = dict(new_result)
        record.duration_seconds = 0
        await session.flush()
        with contextlib.suppress(Exception):
            await _notify_completed_deep_analysis(session, record)
        await session.commit()
        logger.info("[DeepAnalysis] 重试后同步完成 analysis_id=%s engine=%s", record.id, engine_name)
        return {"success": True, "message": "分析已完成"}

    from services.analysis.openclaw import clear_openclaw_poll_state

    _reset_deep_analysis_for_background_poll(record, utcnow())
    await session.flush()
    record_data = deep_analysis_to_dict(record)
    await session.commit()
    with contextlib.suppress(Exception):
        await clear_openclaw_poll_state(int(record.id))
    await taskiq_retry_scheduler.schedule_openclaw_poll_best_effort(int(record.id), 0)
    logger.info("[DeepAnalysis] 已提交后台拉取 analysis_id=%s webhook_id=%s", record.id, record.webhook_event_id)
    return {"success": True, "message": "已提交后台拉取，请稍后刷新查看结果", "data": record_data}


@deep_analysis_router.post(
    "/deep-analyses/{analysis_id}/forward",
    response_model=None,
    dependencies=[Depends(verify_admin_write)],
)
async def forward_deep_analysis(
    analysis_id: int,
    payload: dict[str, Any] | None = None,
    session: AsyncSession = Depends(get_db_session),
    http_client: Any = Depends(get_http_client_dependency),
) -> JSONResponse | JSONDict:
    """转发深度分析结果到指定 URL（飞书卡片或通用 Webhook）"""
    payload = payload or {}
    target_url = (payload.get("target_url") or "").strip()
    logger.info("[DeepAnalysis] 手动转发请求 analysis_id=%s target=%s", analysis_id, mask_url(target_url))
    if not target_url:
        return JSONResponse(status_code=400, content={"success": False, "error": "转发 URL 不能为空"})
    if not target_url.startswith(("http://", "https://")):
        return JSONResponse(status_code=400, content={"success": False, "error": "URL 格式无效"})
    try:
        target_url = await validate_outbound_url(target_url)
    except UnsafeTargetUrlError as e:
        logger.warning("[DeepAnalysis] 手动转发目标 URL 被拒绝 analysis_id=%s error=%s", analysis_id, e)
        return JSONResponse(status_code=400, content={"success": False, "error": TARGET_URL_UNAVAILABLE_MESSAGE})

    analysis = await session.get(DeepAnalysis, analysis_id)
    if not analysis:
        logger.warning("[DeepAnalysis] 手动转发失败，记录不存在 analysis_id=%s", analysis_id)
        return JSONResponse(status_code=404, content={"success": False, "error": "分析记录不存在"})
    if analysis.status != DeepAnalysisStatus.COMPLETED:
        logger.warning("[DeepAnalysis] 手动转发失败，分析未完成 analysis_id=%s status=%s", analysis_id, analysis.status)
        return JSONResponse(status_code=400, content={"success": False, "error": "分析尚未完成"})

    source = "unknown"
    if analysis.webhook_event_id:
        event = await session.get(WebhookEvent, analysis.webhook_event_id)
        if event:
            source = event.source or "unknown"

    from services.forwarding.outbox import forward_notification
    from services.notifications.feishu import build_deep_analysis_card

    is_feishu = is_feishu_url(target_url)
    fwd_payload: dict[str, Any] = {
        "type": "deep_analysis",
        "analysis_id": analysis_id,
        "source": source,
        "engine": analysis.engine,
        "webhook_event_id": analysis.webhook_event_id,
        "analysis_result": analysis.analysis_result,
        "duration_seconds": analysis.duration_seconds,
        "created_at": utc_isoformat(analysis.created_at),
    }

    formatted_payload: JsonObject = (
        build_deep_analysis_card(
            {
                "analysis_result": analysis.analysis_result,
                "engine": analysis.engine,
                "duration_seconds": analysis.duration_seconds,
            },
            source=source,
            webhook_event_id=analysis.webhook_event_id or 0,
        )
        if is_feishu
        else fwd_payload
    )
    try:
        result = await forward_notification(
            event_type="deep_analysis_manual",
            source=source,
            formatted_payload=formatted_payload,
            webhook_id=analysis.webhook_event_id or None,
            target_url=target_url,
            idempotency_extra=f"manual-deep-analysis:{analysis_id}:{uuid4().hex}",
        )
        outbox_id = result.get("outbox_id")
        status = result.get("status", "")
        if status == "skipped":
            reason = result.get("reason", "未知")
            logger.warning(
                "[DeepAnalysis] 手动转发被跳过 analysis_id=%s target=%s reason=%s",
                analysis_id,
                mask_url(target_url),
                reason,
            )
            return JSONResponse(status_code=400, content={"success": False, "error": DELIVERY_ERROR_MESSAGE})
        logger.info(
            "[DeepAnalysis] 手动转发已入队 analysis_id=%s webhook_id=%s outbox_id=%s target=%s",
            analysis_id,
            analysis.webhook_event_id,
            outbox_id,
            mask_url(target_url),
        )
        return {"success": True, "message": "已入队转发", "outbox_id": outbox_id}
    except Exception as e:
        logger.error(
            "[DeepAnalysis] 转发深度分析入队失败 analysis_id=%s target=%s error=%s",
            analysis_id,
            mask_url(target_url),
            e,
        )
        return internal_error_response()

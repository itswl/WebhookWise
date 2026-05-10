import contextlib
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from api.webhook_context import JSONDict, build_webhook_context
from core.config import Config
from core.http_client import get_http_client
from core.logger import logger
from core.utils import is_feishu_url
from db.session import get_db_session
from models import DeepAnalysis, WebhookEvent
from schemas import DeepAnalysisListResponse
from services.analysis.ai_analyzer import analyze_webhook_with_ai, get_deep_analyses_for_webhook, get_deep_analysis_list
from services.forwarding.forward import record_failed_forward
from services.webhooks.types import DeepAnalysisStatus

deep_analysis_router = APIRouter()

MAX_PAGE = 500


def _is_supported_deep_analysis_engine(requested: str) -> bool:
    return requested in ("", "auto", "openclaw")


async def _run_openclaw_deep_analysis(
    ctx: JSONDict, headers: dict[str, Any], user_question: str
) -> tuple[dict[str, Any], str]:
    from services.forwarding.forward import analyze_with_openclaw

    webhook_data: dict[str, Any] = {
        "source": ctx["source"],
        "headers": headers,
        "parsed_data": ctx["parsed_data"],
    }
    result = await analyze_with_openclaw(webhook_data, user_question)
    if result.get("_degraded"):
        logger.warning("[DeepAnalysis] OpenClaw 降级，回退本地 AI: %s", result.get("_degraded_reason"))
        return await analyze_webhook_with_ai(webhook_data), "local (fallback)"
    return result, "openclaw"


async def _notify_completed_deep_analysis(session: AsyncSession, record: DeepAnalysis) -> None:
    from services.analysis.openclaw_poller import notify_deep_analysis_success

    event = await session.get(WebhookEvent, record.webhook_event_id)
    source = event.source if event else ""
    await notify_deep_analysis_success(record, source)


def _prepare_openclaw_poll_if_pending(record: DeepAnalysis) -> int | None:
    if record.status != DeepAnalysisStatus.PENDING:
        return None
    from services.operations.taskiq_retry_scheduler import compute_openclaw_poll_delay

    delay = compute_openclaw_poll_delay(record.poll_attempts or 0)
    record.next_poll_at = datetime.now() + timedelta(seconds=delay)
    return delay


async def _schedule_openclaw_poll_best_effort(analysis_id: int, delay_seconds: int) -> None:
    try:
        from services.operations.taskiq_retry_scheduler import schedule_openclaw_poll

        await schedule_openclaw_poll(analysis_id, delay_seconds)
    except Exception as e:
        logger.warning("[DeepAnalysis] OpenClaw poll 调度失败 analysis_id=%s error=%s", analysis_id, e)


@deep_analysis_router.post("/api/deep-analyze/{webhook_id}", response_model=None)
async def deep_analyze_webhook(
    webhook_id: int, payload: dict[str, Any] | None = None, session: AsyncSession = Depends(get_db_session)
) -> JSONResponse | JSONDict:
    payload = payload or {}
    event = await session.get(WebhookEvent, webhook_id)
    if not event:
        return JSONResponse(status_code=404, content={"success": False, "error": "Webhook not found"})

    ctx = await build_webhook_context(event)
    requested_engine = str(payload.get("engine", "auto")).strip().lower()
    if not _is_supported_deep_analysis_engine(requested_engine):
        return JSONResponse(status_code=400, content={"success": False, "error": "Unsupported engine"})
    if not Config.openclaw.OPENCLAW_ENABLED:
        return JSONResponse(status_code=503, content={"success": False, "error": "No engine available"})

    try:
        res, engine_name = await _run_openclaw_deep_analysis(
            ctx, event.headers or {}, str(payload.get("user_question", ""))
        )
    except Exception as e:
        return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

    record = DeepAnalysis(
        webhook_event_id=webhook_id,
        engine=engine_name,
        user_question=payload.get("user_question", ""),
        analysis_result=res,
        status=DeepAnalysisStatus.PENDING if res.get("_pending") else DeepAnalysisStatus.COMPLETED,
        openclaw_run_id=res.get("_openclaw_run_id", ""),
        openclaw_session_key=res.get("_openclaw_session_key", ""),
    )
    session.add(record)
    await session.flush()
    poll_delay = _prepare_openclaw_poll_if_pending(record)
    await session.commit()
    if poll_delay is not None:
        await _schedule_openclaw_poll_best_effort(record.id, poll_delay)
    return {"success": True, "data": record.to_dict()}


@deep_analysis_router.get("/api/deep-analyses", response_model=DeepAnalysisListResponse)
async def list_all_deep_analyses(
    page: int = Query(1),
    per_page: int = Query(20),
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


@deep_analysis_router.get("/api/deep-analyses/{webhook_id}")
async def get_deep_analyses(webhook_id: int, session: AsyncSession = Depends(get_db_session)) -> JSONDict:
    return {"success": True, "data": await get_deep_analyses_for_webhook(session, webhook_id)}


@deep_analysis_router.post("/api/deep-analyses/{analysis_id}/retry", response_model=None)
async def retry_deep_analysis(
    analysis_id: int, session: AsyncSession = Depends(get_db_session)
) -> JSONResponse | JSONDict:
    """重新拉取或重新发起 OpenClaw 深度分析结果。"""
    record = await session.get(DeepAnalysis, analysis_id)
    if not record:
        return JSONResponse(status_code=404, content={"success": False, "error": "分析记录不存在"})

    if record.status not in (DeepAnalysisStatus.FAILED, DeepAnalysisStatus.COMPLETED, DeepAnalysisStatus.PENDING):
        return JSONResponse(
            status_code=400,
            content={"success": False, "error": f"只能在失败、已完成或待处理状态下重新拉取，当前状态: {record.status}"},
        )

    if not record.openclaw_session_key:
        event = await session.get(WebhookEvent, record.webhook_event_id)
        if not event:
            return JSONResponse(status_code=404, content={"success": False, "error": "关联的 webhook 事件不存在"})

        ctx = await build_webhook_context(event)
        new_result, engine_name = await _run_openclaw_deep_analysis(
            ctx, event.headers or {}, record.user_question or ""
        )
        if new_result.get("_pending"):
            record.status = DeepAnalysisStatus.PENDING
            record.created_at = datetime.now()
            record.analysis_result = new_result
            record.openclaw_run_id = new_result.get("_openclaw_run_id", "")
            record.openclaw_session_key = new_result.get("_openclaw_session_key", "")
            record.duration_seconds = 0
            record.poll_attempts = 0
            record.last_polled_at = None
            await session.flush()
            poll_delay = _prepare_openclaw_poll_if_pending(record)
            await session.commit()
            if poll_delay is not None:
                await _schedule_openclaw_poll_best_effort(record.id, poll_delay)
            return {"success": True, "message": "已重新发起分析任务，请等待结果"}

        record.status = DeepAnalysisStatus.COMPLETED
        record.engine = engine_name
        record.analysis_result = new_result
        record.duration_seconds = 0
        await session.flush()
        with contextlib.suppress(Exception):
            await _notify_completed_deep_analysis(session, record)
        await session.commit()
        return {"success": True, "message": "分析已完成"}

    if Config.openclaw.OPENCLAW_HTTP_API_URL:
        from services.analysis.openclaw_poller import (
            _poll_via_http,
            build_analysis_result_from_openclaw_text,
        )

        result = await _poll_via_http(record.openclaw_session_key, retry_count=3)

        if result.get("status") == "error":
            return JSONResponse(status_code=400, content={"success": False, "error": result.get("error", "获取失败")})
        if result.get("status") != "completed":
            return JSONResponse(
                status_code=400, content={"success": False, "error": f"获取未完成: {result.get('status')}"}
            )

        text = result.get("text", "")
        record.analysis_result = build_analysis_result_from_openclaw_text(text, record.openclaw_run_id or "")
        record.analysis_result["_fetched_via"] = "http-retry"

        record.status = DeepAnalysisStatus.COMPLETED
        record.duration_seconds = (datetime.now() - record.created_at).total_seconds() if record.created_at else 0
        await session.flush()

        try:
            await _notify_completed_deep_analysis(session, record)
        except Exception as e:
            logger.warning("retry: 飞书深度分析通知异常: %s", e, exc_info=True)

        await session.commit()
        return {"success": True, "message": f"获取成功！通过 HTTP API 获取了 {len(text)} 字符的分析结果"}

    timeout_seconds = Config.openclaw.OPENCLAW_TIMEOUT_SECONDS
    elapsed = (datetime.now() - record.created_at).total_seconds() if record.created_at else timeout_seconds + 1
    if elapsed > timeout_seconds:
        record.status = DeepAnalysisStatus.FAILED
        record.analysis_result = {"root_cause": f"OpenClaw 分析超时（已等待 {int(elapsed)}s）"}
        await session.flush()
        await session.commit()
        return JSONResponse(
            status_code=400, content={"success": False, "error": f"分析已超时（{int(elapsed)}s），请重新发起深度分析"}
        )

    record.status = DeepAnalysisStatus.PENDING
    record.analysis_result = None
    record.poll_attempts = 0
    record.last_polled_at = None
    await session.flush()
    poll_delay = _prepare_openclaw_poll_if_pending(record)
    await session.commit()
    if poll_delay is not None:
        await _schedule_openclaw_poll_best_effort(record.id, poll_delay)
    return {"success": True, "message": "已重置为待重试，已调度下一次结果拉取"}


@deep_analysis_router.post("/api/deep-analyses/{analysis_id}/forward", response_model=None)
async def forward_deep_analysis(
    analysis_id: int, payload: dict[str, Any] | None = None, session: AsyncSession = Depends(get_db_session)
) -> JSONResponse | JSONDict:
    """转发深度分析结果到指定 URL（飞书卡片或通用 Webhook）"""
    payload = payload or {}
    target_url = (payload.get("target_url") or "").strip()
    if not target_url:
        return JSONResponse(status_code=400, content={"success": False, "error": "转发 URL 不能为空"})
    if not target_url.startswith(("http://", "https://")):
        return JSONResponse(status_code=400, content={"success": False, "error": "URL 格式无效"})

    analysis = await session.get(DeepAnalysis, analysis_id)
    if not analysis:
        return JSONResponse(status_code=404, content={"success": False, "error": "分析记录不存在"})
    if analysis.status != DeepAnalysisStatus.COMPLETED:
        return JSONResponse(status_code=400, content={"success": False, "error": "分析尚未完成"})

    source = "unknown"
    if analysis.webhook_event_id:
        event = await session.get(WebhookEvent, analysis.webhook_event_id)
        if event:
            source = event.source or "unknown"

    is_feishu = is_feishu_url(target_url)
    if is_feishu:
        from adapters.ecosystem_adapters import send_feishu_deep_analysis

        ok = await send_feishu_deep_analysis(
            webhook_url=target_url,
            analysis_record={
                "analysis_result": analysis.analysis_result,
                "engine": analysis.engine,
                "duration_seconds": analysis.duration_seconds,
            },
            source=source,
            webhook_event_id=analysis.webhook_event_id or 0,
        )
        if ok:
            return {"success": True, "message": "已发送到飞书"}
        with contextlib.suppress(Exception):
            await record_failed_forward(
                webhook_event_id=analysis.webhook_event_id or 0,
                forward_rule_id=None,
                target_url=target_url,
                target_type="feishu",
                failure_reason="feishu_send_failed",
                error_message="深度分析结果飞书发送失败",
                forward_data={"analysis_id": analysis_id, "webhook_event_id": analysis.webhook_event_id},
                session=session,
            )
            await session.commit()
        return JSONResponse(status_code=202, content={"success": True, "message": "分析已提交，飞书通知可能延迟"})

    fwd_payload = {
        "type": "deep_analysis",
        "analysis_id": analysis_id,
        "source": source,
        "engine": analysis.engine,
        "webhook_event_id": analysis.webhook_event_id,
        "analysis_result": analysis.analysis_result,
        "duration_seconds": analysis.duration_seconds,
        "created_at": analysis.created_at.isoformat() if analysis.created_at else None,
    }
    try:
        client = get_http_client()
        resp = await client.post(target_url, json=fwd_payload, timeout=Config.ai.FORWARD_TIMEOUT)
        resp.raise_for_status()
        return {"success": True, "message": f"已转发 (HTTP {resp.status_code})"}
    except Exception as e:
        logger.error("转发深度分析失败: %s", e)
        return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

"""
Analysis API Routes.
Consolidated from deep_analysis, reanalysis, and ai_usage.
"""

import contextlib
import json
import re
import time
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from adapters.ecosystem_adapters import normalize_webhook_event
from adapters.registry import get_default_engine, get_engine
from core.config import policies
from core.http_client import get_http_client
from core.logger import logger, mask_url
from core.redis_client import get_redis
from db.session import get_db_session
from models import DeepAnalysis, WebhookEvent
from schemas import (
    AIUsageResponse,
    DeepAnalysisListResponse,
    ReanalysisResponse,
)
from services.ai_analyzer import (
    analyze_webhook_with_ai,
    get_ai_usage_stats,
    get_deep_analyses_for_webhook,
    get_deep_analysis_list,
)
from services.forward import forward_to_remote, record_failed_forward

analysis_router = APIRouter()

MAX_PAGE = 500


# ── Internal Helpers ─────────────────────────────────────────────────────────


def _resolve_engine(requested: str):
    if requested and requested != "auto":
        engine = get_engine(requested)
        if engine and engine.is_available():
            return engine
    return get_default_engine()


async def _build_webhook_context(event: WebhookEvent) -> dict:
    from services.pipeline import _load_event_payload
    parsed_data, _ = await _load_event_payload(event)
    source = event.source
    if (not source or source == "unknown") and isinstance(parsed_data, dict):
        normalized = normalize_webhook_event(parsed_data, None)
        source, parsed_data = normalized.source or source, normalized.data
    return {
        "source": source, "parsed_data": parsed_data,
        "timestamp": event.timestamp.isoformat() if event.timestamp else None,
        "client_ip": event.client_ip,
    }


# ── Deep Analysis ────────────────────────────────────────────────────────────


@analysis_router.post("/api/deep-analyze/{webhook_id}")
async def deep_analyze_webhook(webhook_id: int, payload: dict = None, session: AsyncSession = Depends(get_db_session)):
    payload = payload or {}
    event = await session.get(WebhookEvent, webhook_id)
    if not event:
        return JSONResponse(status_code=404, content={"success": False, "error": "Webhook not found"})

    ctx = await _build_webhook_context(event)
    engine_impl = _resolve_engine(payload.get("engine", "auto"))
    if not engine_impl:
        return JSONResponse(status_code=503, content={"success": False, "error": "No engine available"})

    try:
        res = await engine_impl.analyze(
            ctx["parsed_data"], source=ctx["source"], headers=event.headers or {}, user_question=payload.get("user_question", "")
        )
    except Exception as e:
        return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

    engine_name = engine_impl.name
    if res.get("_degraded") and engine_name == "openclaw":
        engine_name = "local (fallback)"

    record = DeepAnalysis(
        webhook_event_id=webhook_id, engine=engine_name, user_question=payload.get("user_question", ""),
        analysis_result=res, status="pending" if res.get("_pending") else "completed",
        openclaw_run_id=res.get("_openclaw_run_id", ""), openclaw_session_key=res.get("_openclaw_session_key", "")
    )
    session.add(record)
    await session.flush()
    return {"success": True, "data": record.to_dict()}


@analysis_router.get("/api/deep-analyses", response_model=DeepAnalysisListResponse)
async def list_all_deep_analyses(
    page: int = Query(1), per_page: int = Query(20), cursor: int | None = Query(None),
    status: str = Query(""), engine: str = Query(""), session: AsyncSession = Depends(get_db_session)
):
    try:
        data = await get_deep_analysis_list(session, page, per_page, cursor, status, engine, MAX_PAGE)
        return {"success": True, "data": data}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@analysis_router.get("/api/deep-analyses/{webhook_id}")
async def get_deep_analyses(webhook_id: int, session: AsyncSession = Depends(get_db_session)):
    return {"success": True, "data": await get_deep_analyses_for_webhook(session, webhook_id)}


@analysis_router.post("/api/deep-analyses/{analysis_id}/retry")
async def retry_deep_analysis(analysis_id: int, session: AsyncSession = Depends(get_db_session)):
    """重新拉取深度分析结果（恢复旧版完整逻辑）"""
    from core.compression import decompress_payload_async

    record = await session.get(DeepAnalysis, analysis_id)
    if not record:
        return JSONResponse(status_code=404, content={"success": False, "error": "分析记录不存在"})

    if record.status not in ("failed", "completed", "pending"):
        return JSONResponse(
            status_code=400,
            content={"success": False, "error": f"只能在失败、已完成或待处理状态下重新拉取，当前状态: {record.status}"},
        )

    if not record.openclaw_session_key:
        # 没有 session key，重新调用 OpenClaw 获取新的 session_key
        event = await session.get(WebhookEvent, record.webhook_event_id)
        if not event:
            return JSONResponse(status_code=404, content={"success": False, "error": "关联的 webhook 事件不存在"})

        alert_data = event.parsed_data or {}
        if not alert_data and event.raw_payload:
            try:
                raw_text = await decompress_payload_async(event.raw_payload) or ""
                import orjson
                alert_data = orjson.loads(raw_text)
            except Exception:
                alert_data = {}

        webhook_data = {"source": event.source or "unknown", "headers": event.headers or {}, "parsed_data": alert_data}

        from services.forward import analyze_with_openclaw
        new_result = await analyze_with_openclaw(webhook_data, user_question=record.user_question or "")

        if new_result.get("_pending"):
            record.status = "pending"
            record.created_at = datetime.now()
            record.analysis_result = new_result
            record.openclaw_run_id = new_result.get("_openclaw_run_id", "")
            record.openclaw_session_key = new_result.get("_openclaw_session_key", "")
            record.duration_seconds = 0
            await session.flush()
            return {"success": True, "message": "已重新发起分析任务，请等待结果"}
        else:
            record.status = "completed"
            record.analysis_result = new_result
            record.duration_seconds = 0
            await session.flush()
            # 发送飞书通知
            try:
                from adapters.ecosystem_adapters import send_feishu_deep_analysis
                if policies.ai.DEEP_ANALYSIS_FEISHU_WEBHOOK:
                    fwd_event = await session.get(WebhookEvent, record.webhook_event_id)
                    fwd_source = fwd_event.source if fwd_event else ""
                    await send_feishu_deep_analysis(
                        webhook_url=policies.ai.DEEP_ANALYSIS_FEISHU_WEBHOOK,
                        analysis_record={"analysis_result": record.analysis_result, "engine": record.engine, "duration_seconds": 0},
                        source=fwd_source, webhook_event_id=record.webhook_event_id,
                    )
            except Exception as _fe:
                logger.warning("retry: 飞书深度分析通知失败: %s", _fe)
            return {"success": True, "message": "分析已完成"}

    # 配置了 HTTP API URL：直接拉取
    if policies.openclaw.OPENCLAW_HTTP_API_URL:
        from services.openclaw_poller import _poll_via_http
        result = await _poll_via_http(record.openclaw_session_key, retry_count=3)

        if result.get("status") == "error":
            return JSONResponse(status_code=400, content={"success": False, "error": result.get("error", "获取失败")})
        if result.get("status") != "completed":
            return JSONResponse(status_code=400, content={"success": False, "error": f"获取未完成: {result.get('status')}"}  )

        text = result.get("text", "")
        parsed_result = None
        json_match = re.search(r"\{[\s\S]*\}", text)
        if json_match:
            with contextlib.suppress(json.JSONDecodeError):
                parsed_result = json.loads(json_match.group())

        if parsed_result and isinstance(parsed_result, dict):
            parsed_result.update({"_openclaw_run_id": record.openclaw_run_id, "_openclaw_text": text, "_fetched_via": "http-retry"})
            record.analysis_result = parsed_result
        else:
            record.analysis_result = {
                "root_cause": text, "impact": "", "recommendations": [], "confidence": 0.5,
                "_openclaw_run_id": record.openclaw_run_id, "_openclaw_text": text, "_fetched_via": "http-retry",
            }

        record.status = "completed"
        record.duration_seconds = (datetime.now() - record.created_at).total_seconds() if record.created_at else 0
        await session.flush()

        # 发送飞书通知
        try:
            from adapters.ecosystem_adapters import send_feishu_deep_analysis
            feishu_url = policies.ai.DEEP_ANALYSIS_FEISHU_WEBHOOK
            logger.info("retry: 准备发送飞书通知, webhook_url=%s, analysis_id=%s", bool(feishu_url), analysis_id)
            if feishu_url:
                event = await session.get(WebhookEvent, record.webhook_event_id)
                source = event.source if event else ""
                ok = await send_feishu_deep_analysis(
                    webhook_url=feishu_url,
                    analysis_record={"analysis_result": record.analysis_result, "engine": record.engine, "duration_seconds": record.duration_seconds},
                    source=source, webhook_event_id=record.webhook_event_id,
                )
                if ok:
                    logger.info("retry: 飞书通知发送成功, analysis_id=%s", analysis_id)
                else:
                    logger.warning("retry: 飞书通知发送失败(返回False), analysis_id=%s", analysis_id)
                    with contextlib.suppress(Exception):
                        await record_failed_forward(
                            webhook_event_id=record.webhook_event_id, forward_rule_id=None,
                            target_url=feishu_url, target_type="feishu",
                            failure_reason="feishu_send_failed", error_message="HTTP重试后飞书通知发送失败",
                            forward_data={"analysis_id": analysis_id, "webhook_event_id": record.webhook_event_id},
                            session=session,
                        )
            else:
                logger.warning("retry: DEEP_ANALYSIS_FEISHU_WEBHOOK 未配置，跳过通知")
        except Exception as e:
            logger.warning("retry: 飞书深度分析通知异常: %s", e, exc_info=True)

        return {"success": True, "message": f"获取成功！通过 HTTP API 获取了 {len(text)} 字符的分析结果"}

    # 无 HTTP API：检查是否已超时，超时直接标记 failed，否则重置 pending 让轮询器处理
    timeout_seconds = policies.openclaw.OPENCLAW_TIMEOUT_SECONDS
    elapsed = (datetime.now() - record.created_at).total_seconds() if record.created_at else timeout_seconds + 1
    if elapsed > timeout_seconds:
        record.status = "failed"
        record.analysis_result = {"root_cause": f"OpenClaw 分析超时（已等待 {int(elapsed)}s）"}
        await session.flush()
        return JSONResponse(status_code=400, content={"success": False, "error": f"分析已超时（{int(elapsed)}s），请重新发起深度分析"})
    # 未超时：重置 pending，保留原始 created_at 让超时检测继续生效
    record.status = "pending"
    record.analysis_result = None
    await session.flush()
    return {"success": True, "message": "已重置为待重试，将在下次轮询时拉取结果"}


@analysis_router.post("/api/deep-analyses/{analysis_id}/forward")
async def forward_deep_analysis(
    analysis_id: int, payload: dict | None = None, session: AsyncSession = Depends(get_db_session)
):
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
    if analysis.status != "completed":
        return JSONResponse(status_code=400, content={"success": False, "error": "分析尚未完成"})

    source = "unknown"
    if analysis.webhook_event_id:
        event = await session.get(WebhookEvent, analysis.webhook_event_id)
        if event:
            source = event.source or "unknown"

    is_feishu = "feishu.cn" in target_url or "larksuite.com" in target_url
    if is_feishu:
        from adapters.ecosystem_adapters import send_feishu_deep_analysis
        ok = await send_feishu_deep_analysis(
            webhook_url=target_url,
            analysis_record={"analysis_result": analysis.analysis_result, "engine": analysis.engine, "duration_seconds": analysis.duration_seconds},
            source=source, webhook_event_id=analysis.webhook_event_id or 0,
        )
        if ok:
            return {"success": True, "message": "已发送到飞书"}
        with contextlib.suppress(Exception):
            await record_failed_forward(
                webhook_event_id=analysis.webhook_event_id or 0, forward_rule_id=None,
                target_url=target_url, target_type="feishu", failure_reason="feishu_send_failed",
                error_message="深度分析结果飞书发送失败",
                forward_data={"analysis_id": analysis_id, "webhook_event_id": analysis.webhook_event_id},
                session=session,
            )
        return JSONResponse(status_code=202, content={"success": True, "message": "分析已提交，飞书通知可能延迟"})
    else:
        fwd_payload = {
            "type": "deep_analysis", "analysis_id": analysis_id, "source": source,
            "engine": analysis.engine, "webhook_event_id": analysis.webhook_event_id,
            "analysis_result": analysis.analysis_result, "duration_seconds": analysis.duration_seconds,
            "created_at": analysis.created_at.isoformat() if analysis.created_at else None,
        }
        try:
            client = get_http_client()
            resp = await client.post(target_url, json=fwd_payload, timeout=policies.ai.FORWARD_TIMEOUT)
            resp.raise_for_status()
            return {"success": True, "message": f"已转发 (HTTP {resp.status_code})"}
        except Exception as e:
            logger.error("转发深度分析失败: %s", e)
            return JSONResponse(status_code=500, content={"success": False, "error": str(e)})


# ── Reanalysis & Manual Forward ──────────────────────────────────────────────


@analysis_router.post("/api/reanalyze/{webhook_id}", response_model=ReanalysisResponse)
async def reanalyze_webhook(webhook_id: int, session: AsyncSession = Depends(get_db_session)):
    event = await session.get(WebhookEvent, webhook_id)
    if not event:
        raise HTTPException(404, "Webhook not found")

    ctx = await _build_webhook_context(event)
    res = await analyze_webhook_with_ai(ctx, skip_cache=True)

    old_imp, new_imp = event.importance, res.get("importance")
    event.ai_analysis, event.importance = res, new_imp
    event.processing_status = "completed"

    updated_dups = 0
    if event.is_duplicate == 0:
        dups_stmt = select(WebhookEvent).filter(WebhookEvent.duplicate_of == webhook_id)
        dups_res = await session.execute(dups_stmt)
        dups = dups_res.scalars().all()
        for d in dups:
            d.ai_analysis, d.importance = res, new_imp
            d.processing_status = "completed"
        updated_dups = len(dups)

    # 重新分析完成后触发转发通知
    try:
        fwd_ctx = await _build_webhook_context(event)
        await forward_to_remote(fwd_ctx, res)
    except Exception as e:
        logger.warning("reanalyze: 转发通知失败: %s", e)

    return {
        "success": True, "status": "success", "analysis": res,
        "original_importance": old_imp, "new_importance": new_imp,
        "updated_duplicates": updated_dups, "message": "重新分析完成"
    }


@analysis_router.post("/api/forward/{webhook_id}")
async def manual_forward_webhook(
    webhook_id: int, data: dict | None = None, session: AsyncSession = Depends(get_db_session)
):
    data = data or {}
    event = await session.get(WebhookEvent, webhook_id)
    if not event:
        raise HTTPException(404, "Webhook not found")

    ctx = await _build_webhook_context(event)
    url = data.get("target_url")
    fwd_res = await forward_to_remote(ctx, event.ai_analysis or {}, url)
    event.forward_status = fwd_res.get("status", "unknown")

    return {"success": True, "data": fwd_res, "message": f"已转发至 {mask_url(url or policies.ai.FORWARD_URL)}"}


# ── AI Usage ─────────────────────────────────────────────────────────────────


@analysis_router.get("/api/ai-usage", response_model=AIUsageResponse)
async def get_ai_usage_endpoint(period: str = Query("day"), session: AsyncSession = Depends(get_db_session)):
    cache_key = f"api:ai_usage:{period}:{int(time.time() // 60)}"
    redis = get_redis()
    with contextlib.suppress(Exception):
        cached = await redis.get(cache_key)
        if cached:
            return {"success": True, "data": json.loads(cached)}

    data = await get_ai_usage_stats(session, period)
    with contextlib.suppress(Exception):
        await redis.setex(cache_key, 70, json.dumps(data))
    return {"success": True, "data": data}

"""
Analysis API Routes.
Consolidated from deep_analysis, reanalysis, and ai_usage.
"""

import contextlib
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from adapters.registry import get_default_engine, get_engine
from adapters.ecosystem_adapters import normalize_webhook_event
from core.compression import decompress_payload_async
from core.config import Config, policies
from core.http_client import get_http_client
from core.logger import logger, mask_url
from core.redis_client import get_redis
from db.session import get_db_session
from models import DeepAnalysis, WebhookEvent
from schemas import (
    AIUsageResponse,
    DeepAnalysisListResponse,
    DeepAnalysisRecord,
    ReanalysisResponse,
)
from services.ai_analyzer import (
    analyze_webhook_with_ai,
    get_ai_usage_stats,
    get_deep_analyses_for_webhook,
    get_deep_analysis_list,
)
from services.forward import record_failed_forward, forward_to_remote

analysis_router = APIRouter()

MAX_PAGE = 500


# ── Internal Helpers ─────────────────────────────────────────────────────────


def _resolve_engine(requested: str):
    if requested and requested != "auto":
        engine = get_engine(requested)
        if engine and engine.is_available(): return engine
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
    if not event: return JSONResponse(status_code=404, content={"success": False, "error": "Webhook not found"})

    ctx = await _build_webhook_context(event)
    engine_impl = _resolve_engine(payload.get("engine", "auto"))
    if not engine_impl: return JSONResponse(status_code=503, content={"success": False, "error": "No engine available"})

    try:
        res = await engine_impl.analyze(ctx["parsed_data"], source=ctx["source"], headers=event.headers or {}, user_question=payload.get("user_question", ""))
    except Exception as e: return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

    engine_name = engine_impl.name
    if res.get("_degraded") and engine_name == "openclaw": engine_name = "local (fallback)"

    record = DeepAnalysis(webhook_event_id=webhook_id, engine=engine_name, user_question=payload.get("user_question", ""), analysis_result=res, status="pending" if res.get("_pending") else "completed", openclaw_run_id=res.get("_openclaw_run_id", ""), openclaw_session_key=res.get("_openclaw_session_key", ""))
    session.add(record); await session.flush()
    return {"success": True, "data": record}


@analysis_router.get("/api/deep-analyses", response_model=DeepAnalysisListResponse)
async def list_all_deep_analyses(page: int = Query(1), per_page: int = Query(20), cursor: int | None = Query(None), status: str = Query(""), engine: str = Query(""), session: AsyncSession = Depends(get_db_session)):
    try:
        data = await get_deep_analysis_list(session, page, per_page, cursor, status, engine, MAX_PAGE)
        return {"success": True, "data": data}
    except ValueError as e: raise HTTPException(status_code=400, detail=str(e))


@analysis_router.get("/api/deep-analyses/{webhook_id}")
async def get_deep_analyses(webhook_id: int, session: AsyncSession = Depends(get_db_session)):
    return {"success": True, "data": await get_deep_analyses_for_webhook(session, webhook_id)}


# ── Reanalysis & Manual Forward ──────────────────────────────────────────────


@analysis_router.post("/api/reanalyze/{webhook_id}", response_model=ReanalysisResponse)
async def reanalyze_webhook(webhook_id: int, session: AsyncSession = Depends(get_db_session)):
    event = await session.get(WebhookEvent, webhook_id)
    if not event: raise HTTPException(404, "Webhook not found")

    ctx = await _build_webhook_context(event)
    res = await analyze_webhook_with_ai(ctx, skip_cache=True)
    
    old_imp, new_imp = event.importance, res.get("importance")
    event.ai_analysis, event.importance = res, new_imp

    updated_dups = 0
    if event.is_duplicate == 0:
        dups = (await session.execute(select(WebhookEvent).filter(WebhookEvent.duplicate_of == webhook_id))).scalars().all()
        for d in dups: d.ai_analysis, d.importance = res, new_imp
        updated_dups = len(dups)

    return {"success": True, "status": "success", "analysis": res, "original_importance": old_imp, "new_importance": new_imp, "updated_duplicates": updated_dups, "message": "重新分析完成"}


@analysis_router.post("/api/forward/{webhook_id}")
async def manual_forward_webhook(webhook_id: int, data: dict | None = None, session: AsyncSession = Depends(get_db_session)):
    data = data or {}
    event = await session.get(WebhookEvent, webhook_id)
    if not event: raise HTTPException(404, "Webhook not found")

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
        if cached: return {"success": True, "data": json.loads(cached)}

    data = await get_ai_usage_stats(session, period)
    with contextlib.suppress(Exception): await redis.setex(cache_key, 70, json.dumps(data))
    return {"success": True, "data": data}

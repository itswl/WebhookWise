from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.webhook_context import JSONDict, build_webhook_context
from core.config import Config
from core.logger import logger, mask_url
from db.session import get_db_session
from models import WebhookEvent
from schemas import ReanalysisResponse
from services.analysis.ai_analyzer import analyze_webhook_with_ai
from services.forwarding.forward import forward_to_remote
from services.webhooks.pipeline import _decide_forwarding, _execute_forwarding

reanalysis_router = APIRouter()


@reanalysis_router.post("/api/reanalyze/{webhook_id}", response_model=ReanalysisResponse)
async def reanalyze_webhook(webhook_id: int, session: AsyncSession = Depends(get_db_session)) -> JSONDict:
    event = await session.get(WebhookEvent, webhook_id)
    if not event:
        raise HTTPException(404, "Webhook not found")

    ctx = await build_webhook_context(event)
    res = await analyze_webhook_with_ai(ctx, skip_cache=True)

    old_imp, new_imp = event.importance, res.get("importance")
    event.ai_analysis, event.importance = res, new_imp
    event.processing_status = "completed"

    updated_dups = 0
    if event.is_duplicate is False:
        dups_stmt = select(WebhookEvent).filter(WebhookEvent.duplicate_of == webhook_id)
        dups_res = await session.execute(dups_stmt)
        dups = dups_res.scalars().all()
        for d in dups:
            d.ai_analysis, d.importance = res, new_imp
            d.processing_status = "completed"
        updated_dups = len(dups)

    try:
        fwd_ctx = await build_webhook_context(event)
        decision = await _decide_forwarding(
            importance=new_imp or "medium",
            is_duplicate=event.is_duplicate,
            beyond_window=event.beyond_window,
            noise=None,
            orig=None,
            source=event.source or "unknown",
        )
        if decision.should_forward:
            await _execute_forwarding(decision, fwd_ctx, res, event.id, orig_id=None)
        else:
            logger.info("reanalyze: 根据规则跳过转发 reason=%s", decision.skip_reason)
    except Exception as e:
        logger.warning("reanalyze: 转发通知失败: %s", e)

    await session.commit()
    return {
        "success": True,
        "status": "success",
        "analysis": res,
        "original_importance": old_imp,
        "new_importance": new_imp,
        "updated_duplicates": updated_dups,
        "message": "重新分析完成",
    }


@reanalysis_router.post("/api/forward/{webhook_id}", response_model=None)
async def manual_forward_webhook(
    webhook_id: int, data: dict[str, Any] | None = None, session: AsyncSession = Depends(get_db_session)
) -> JSONDict | JSONResponse:
    data = data or {}
    event = await session.get(WebhookEvent, webhook_id)
    if not event:
        raise HTTPException(404, "Webhook not found")

    ctx = await build_webhook_context(event)
    url = data.get("target_url") or data.get("forward_url")
    fwd_res = await forward_to_remote(ctx, event.ai_analysis or {}, url)
    event.forward_status = fwd_res.get("status", "unknown")
    await session.commit()

    status = str(fwd_res.get("status", "unknown"))
    if status != "success":
        http_status = 400 if status in {"invalid_target", "skipped"} else 502
        return JSONResponse(
            status_code=http_status,
            content={
                "success": False,
                "data": fwd_res,
                "error": fwd_res.get("message") or fwd_res.get("reason") or f"转发失败: {status}",
            },
        )

    return {"success": True, "data": fwd_res, "message": f"已转发至 {mask_url(url or Config.ai.FORWARD_URL)}"}

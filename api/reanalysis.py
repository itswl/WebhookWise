from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.webhook_context import JSONDict, build_webhook_context
from core.auth import verify_admin_write
from core.logger import get_logger, mask_url
from db.session import get_db_session
from models import WebhookEvent
from schemas import ReanalysisResponse
from services.analysis.ai_analyzer import analyze_webhook_with_ai
from services.forwarding.outbox import create_forward_outbox_records, schedule_forward_outbox_many
from services.forwarding.policies import RemoteForwardPolicy
from services.forwarding.remote import forward_to_remote
from services.webhooks.forwarding_stage import resolve_forward_decision
from services.webhooks.types import AnalysisResult

logger = get_logger("api.reanalysis")

reanalysis_router = APIRouter()


@reanalysis_router.post(
    "/api/reanalyze/{webhook_id}",
    response_model=ReanalysisResponse,
    dependencies=[Depends(verify_admin_write)],
)
async def reanalyze_webhook(webhook_id: int, session: AsyncSession = Depends(get_db_session)) -> JSONDict:
    logger.info("[Reanalysis] 重新分析请求 webhook_id=%s", webhook_id)
    event = await session.get(WebhookEvent, webhook_id)
    if not event:
        logger.warning("[Reanalysis] 重新分析失败，事件不存在 webhook_id=%s", webhook_id)
        raise HTTPException(404, "Webhook not found")

    ctx = await build_webhook_context(event)
    res = await analyze_webhook_with_ai(ctx, skip_cache=True)

    old_imp, new_imp = event.importance, res.get("importance")
    event.ai_analysis, event.importance = dict(res), new_imp
    event.processing_status = "completed"

    updated_dups = 0
    if event.is_duplicate is False:
        dups_stmt = select(WebhookEvent).filter(WebhookEvent.duplicate_of == webhook_id)
        dups_res = await session.execute(dups_stmt)
        dups = dups_res.scalars().all()
        for d in dups:
            d.ai_analysis, d.importance = dict(res), new_imp
            d.processing_status = "completed"
        updated_dups = len(dups)

    fwd_ctx = await build_webhook_context(event)
    decision = await resolve_forward_decision(
        importance=new_imp or "medium",
        is_duplicate=bool(event.is_duplicate),
        noise=None,
        orig=None,
        source=event.source or "unknown",
        parsed_data=cast(dict[str, Any], fwd_ctx.get("parsed_data") or {}),
        session=session,
    )
    outbox_ids: list[int] = []
    if decision.should_forward:
        outbox_ids = await create_forward_outbox_records(
            session,
            decision=decision,
            full_data=fwd_ctx,
            analysis=res,
            webhook_id=event.id,
            orig_id=None,
        )
    else:
        logger.info("[Reanalysis] 根据规则跳过转发 webhook_id=%s reason=%s", webhook_id, decision.skip_reason)

    await session.commit()
    await schedule_forward_outbox_many(outbox_ids)
    logger.info(
        "[Reanalysis] 重新分析完成 webhook_id=%s source=%s old_importance=%s new_importance=%s updated_duplicates=%s outboxes=%s",
        webhook_id,
        event.source,
        old_imp,
        new_imp,
        updated_dups,
        len(outbox_ids),
    )
    return {
        "success": True,
        "status": "success",
        "analysis": res,
        "original_importance": old_imp,
        "new_importance": new_imp,
        "updated_duplicates": updated_dups,
        "forward_status": "queued" if outbox_ids else ("skipped" if not decision.should_forward else "no_target"),
        "forward_outbox_ids": outbox_ids,
        "message": "重新分析完成",
    }


@reanalysis_router.post(
    "/api/forward/{webhook_id}",
    response_model=None,
    dependencies=[Depends(verify_admin_write)],
)
async def manual_forward_webhook(
    webhook_id: int, data: dict[str, Any] | None = None, session: AsyncSession = Depends(get_db_session)
) -> JSONDict | JSONResponse:
    data = data or {}
    logger.info("[Reanalysis] 手动转发请求 webhook_id=%s", webhook_id)
    event = await session.get(WebhookEvent, webhook_id)
    if not event:
        logger.warning("[Reanalysis] 手动转发失败，事件不存在 webhook_id=%s", webhook_id)
        raise HTTPException(404, "Webhook not found")

    ctx = await build_webhook_context(event)
    url = data.get("target_url")
    fwd_res = await forward_to_remote(ctx, cast(AnalysisResult, event.ai_analysis or {}), url)
    event.forward_status = fwd_res.get("status", "unknown")
    await session.commit()

    status = str(fwd_res.get("status", "unknown"))
    if status != "success":
        logger.warning(
            "[Reanalysis] 手动转发失败 webhook_id=%s status=%s message=%s", webhook_id, status, fwd_res.get("message")
        )
        http_status = 400 if status in {"invalid_target", "skipped"} else 502
        return JSONResponse(
            status_code=http_status,
            content={
                "success": False,
                "data": fwd_res,
                "error": fwd_res.get("message") or fwd_res.get("reason") or f"转发失败: {status}",
            },
        )

    target_url = url or RemoteForwardPolicy.from_config().default_target_url
    logger.info("[Reanalysis] 手动转发完成 webhook_id=%s target=%s", webhook_id, mask_url(target_url))
    return {"success": True, "data": fwd_res, "message": f"已转发至 {mask_url(target_url)}"}

"""
api/reanalysis.py
=========================
重新分析 + 手动转发路由。
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import Config
from core.logger import logger
from db.session import get_db_session
from models import WebhookEvent
from services.ai_analyzer import analyze_webhook_with_ai
from services.forward import forward_to_remote

reanalysis_router = APIRouter()


# ── 辅助函数 ─────────────────────────────────────────────────────────────────


async def _get_webhook_event_by_id(session, webhook_id: int) -> WebhookEvent | None:
    result = await session.execute(select(WebhookEvent).filter_by(id=webhook_id))
    return result.scalars().first()


async def _build_webhook_context(event: WebhookEvent) -> dict:
    return {
        "source": event.source,
        "parsed_data": event.parsed_data,
        "timestamp": event.timestamp.isoformat() if event.timestamp else None,
        "client_ip": event.client_ip,
    }


async def _propagate_analysis_to_duplicates(
    session, webhook_id: int, analysis_result: dict, new_importance: str | None
) -> int:
    stmt = select(WebhookEvent).filter(WebhookEvent.duplicate_of == webhook_id)
    result = await session.execute(stmt)
    duplicate_events = result.scalars().all()
    for dup in duplicate_events:
        dup.ai_analysis = analysis_result
        dup.importance = new_importance
    return len(duplicate_events)


async def _reanalyze_webhook_event(
    session, webhook_event: WebhookEvent, webhook_id: int
) -> tuple[dict, str | None, str | None, int]:
    webhook_data = await _build_webhook_context(webhook_event)

    logger.info(f"重新分析 webhook ID: {webhook_id}")
    analysis_result = await analyze_webhook_with_ai(webhook_data, skip_cache=True)

    old_importance = webhook_event.importance
    new_importance = analysis_result.get("importance")

    webhook_event.ai_analysis = analysis_result
    webhook_event.importance = new_importance

    logger.info(f"重新分析完成: {old_importance} → {new_importance} - {analysis_result.get('summary', '')}")

    updated_duplicates = 0
    if webhook_event.is_duplicate == 0:
        updated_duplicates = await _propagate_analysis_to_duplicates(
            session, webhook_id, analysis_result, new_importance
        )
        if updated_duplicates:
            logger.info(f"同时更新了 {updated_duplicates} 条重复告警的分析结果")

    return analysis_result, old_importance, new_importance, updated_duplicates


async def _manual_forward(session, webhook_event: WebhookEvent, webhook_id: int, custom_url) -> dict:
    webhook_data = await _build_webhook_context(webhook_event)
    analysis_result = webhook_event.ai_analysis or {}

    logger.info(f"手动转发 webhook ID: {webhook_id} 到 {custom_url or Config.ai.FORWARD_URL}")
    forward_result = await forward_to_remote(webhook_data, analysis_result, custom_url)

    webhook_event.forward_status = forward_result.get("status", "unknown")
    return forward_result


# ── 路由 ─────────────────────────────────────────────────────────────────────


@reanalysis_router.post("/api/reanalyze/{webhook_id}")
async def reanalyze_webhook(webhook_id: int, session: AsyncSession = Depends(get_db_session)):
    """重新分析指定的 webhook，并更新所有引用它的重复告警"""
    try:
        webhook_event = await _get_webhook_event_by_id(session, webhook_id)
        if not webhook_event:
            raise HTTPException(status_code=404, detail="Webhook not found")

        analysis_result, old_importance, new_importance, updated_duplicates = await _reanalyze_webhook_event(
            session, webhook_event, webhook_id
        )

        return {
            "success": True,
            "status": 200,
            "analysis": analysis_result,
            "original_importance": old_importance,
            "new_importance": new_importance,
            "updated_duplicates": updated_duplicates,
            "message": f"重新分析完成，importance: {old_importance} → {new_importance}"
            + (f"，同时更新了 {updated_duplicates} 条重复告警" if updated_duplicates > 0 else ""),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"重新分析失败: {e!s}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e


@reanalysis_router.post("/api/forward/{webhook_id}")
async def manual_forward_webhook(
    webhook_id: int, data: dict | None = None, session: AsyncSession = Depends(get_db_session)
):
    data = data or {}
    """手动转发 webhook"""
    try:
        custom_url = data.get("target_url")

        webhook_event = await _get_webhook_event_by_id(session, webhook_id)
        if not webhook_event:
            raise HTTPException(status_code=404, detail="Webhook not found")

        forward_result = await _manual_forward(session, webhook_event, webhook_id, custom_url)

        return {"success": True, "data": forward_result, "message": f"已转发至 {custom_url or Config.ai.FORWARD_URL}"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"手动转发失败: {e!s}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e

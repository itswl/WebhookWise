from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from api import DELIVERY_ERROR_MESSAGE, TARGET_URL_UNAVAILABLE_MESSAGE, internal_error_response
from api.v1.webhook import JSONDict
from core.auth import verify_admin_write
from core.logger import get_logger, mask_url
from core.url_security import UnsafeTargetUrlError, validate_outbound_url
from db.session import get_db_session
from models import WebhookEvent
from schemas.analysis import ReanalysisResponse
from services.forwarding.outbox import forward_notification
from services.webhooks.event_context import build_webhook_context
from services.webhooks.reanalysis_service import WebhookEventNotFoundError, reanalyze_webhook_event
from services.webhooks.types import AnalysisResult

logger = get_logger("api.v1.reanalysis")

reanalysis_router = APIRouter()
_REANALYSIS_RUNTIME_ERRORS = (OSError, RuntimeError, SQLAlchemyError, TimeoutError, ValueError)


@reanalysis_router.post(
    "/reanalyze/{webhook_id}",
    response_model=ReanalysisResponse,
    dependencies=[Depends(verify_admin_write)],
)
async def reanalyze_webhook(
    webhook_id: int, session: AsyncSession = Depends(get_db_session)
) -> JSONDict | JSONResponse:
    try:
        logger.info("[Reanalysis] 重新分析请求 webhook_id=%s", webhook_id)
        try:
            result = await reanalyze_webhook_event(session, webhook_id)
        except WebhookEventNotFoundError:
            logger.warning("[Reanalysis] 重新分析失败，事件不存在 webhook_id=%s", webhook_id)
            raise HTTPException(404, "Webhook not found") from None

        return {
            "success": True,
            "status": "success",
            "analysis": result.analysis,
            "original_importance": result.original_importance,
            "new_importance": result.new_importance,
            "updated_duplicates": result.updated_duplicates,
            "forward_status": result.forward_status,
            "forward_outbox_ids": result.outbox_ids,
            "message": "重新分析完成",
        }
    except HTTPException:
        raise
    except _REANALYSIS_RUNTIME_ERRORS as e:
        logger.error("[Reanalysis] 重新分析异常 webhook_id=%s error=%s", webhook_id, e, exc_info=True)
        return internal_error_response()


@reanalysis_router.post(
    "/forward/{webhook_id}",
    response_model=None,
    dependencies=[Depends(verify_admin_write)],
)
async def manual_forward_webhook(
    webhook_id: int, data: dict[str, Any] | None = None, session: AsyncSession = Depends(get_db_session)
) -> JSONDict | JSONResponse:
    try:
        data = data or {}
        target_url = str(data.get("target_url", "")).strip() if data.get("target_url") else ""
        logger.info(
            "[Reanalysis] 手动转发请求 webhook_id=%s target=%s",
            webhook_id,
            mask_url(target_url) if target_url else "(rule-based)",
        )
        event = await session.get(WebhookEvent, webhook_id)
        if not event:
            logger.warning("[Reanalysis] 手动转发失败，事件不存在 webhook_id=%s", webhook_id)
            raise HTTPException(404, "Webhook not found")

        if target_url:
            if not target_url.startswith(("http://", "https://")):
                return JSONResponse(status_code=400, content={"success": False, "error": "URL 格式无效"})
            try:
                target_url = await validate_outbound_url(target_url)
            except UnsafeTargetUrlError as e:
                logger.warning("[Reanalysis] 手动转发目标 URL 被拒绝 webhook_id=%s error=%s", webhook_id, e)
                return JSONResponse(
                    status_code=400, content={"success": False, "error": TARGET_URL_UNAVAILABLE_MESSAGE}
                )

        ctx = await build_webhook_context(event)
        fwd_res = await forward_notification(
            event_type="manual_forward",
            source=event.source or "unknown",
            forward_data=ctx,
            analysis_result=cast(AnalysisResult, event.ai_analysis or {}),
            webhook_id=event.id,
            wait=True,
            target_url=target_url,
            importance=event.importance or "",
            is_duplicate=bool(event.is_duplicate),
            parsed_data=cast(dict[str, Any], ctx.get("parsed_data") or {}),
        )
        event.forward_status = fwd_res.get("status", "unknown")
        await session.commit()

        status = str(fwd_res.get("status", "unknown"))
        if status != "success":
            logger.warning(
                "[Reanalysis] 手动转发失败 webhook_id=%s status=%s message=%s reason=%s",
                webhook_id,
                status,
                fwd_res.get("message"),
                fwd_res.get("reason"),
            )
            http_status = 400 if status == "skipped" else 502
            return JSONResponse(
                status_code=http_status,
                content={"success": False, "error": "转发已跳过" if status == "skipped" else DELIVERY_ERROR_MESSAGE},
            )

        logger.info("[Reanalysis] 手动转发完成 webhook_id=%s result=%s", webhook_id, fwd_res.get("status"))
        return {"success": True, "data": fwd_res, "message": "转发完成"}
    except HTTPException:
        raise
    except _REANALYSIS_RUNTIME_ERRORS as e:
        logger.error("[Reanalysis] 手动转发异常 webhook_id=%s error=%s", webhook_id, e, exc_info=True)
        return internal_error_response()

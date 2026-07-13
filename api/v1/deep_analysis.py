from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from api import DELIVERY_ERROR_MESSAGE, TARGET_URL_UNAVAILABLE_MESSAGE, internal_error_response
from api.v1.webhook import JSONDict
from core.auth import verify_admin_write, verify_api_key
from core.logger import get_logger, mask_url
from core.url_security import UnsafeTargetUrlError
from db.session import get_db_session
from models import DeepAnalysis, WebhookEvent
from schemas.analysis import DeepAnalysisListResponse, deep_analysis_to_dict
from services.analysis import deep_analysis_workflow
from services.analysis.analysis_queries import get_deep_analyses_for_webhook, get_deep_analysis_list
from services.forwarding.policies import OpenClawTriggerPolicy
from services.operations import taskiq_retry_scheduler
from services.webhooks.types import (
    DeepAnalysisStatus,
    is_pending_result,
    openclaw_run_id,
    openclaw_session_key,
)

logger = get_logger("api.v1.deep_analysis")

deep_analysis_router = APIRouter()

MAX_PAGE = 500
_build_deep_analysis_context = deep_analysis_workflow.build_deep_analysis_context
_forward_deep_analysis_record = deep_analysis_workflow.forward_deep_analysis_record
_is_supported_deep_analysis_engine = deep_analysis_workflow.is_supported_deep_analysis_engine
_prepare_openclaw_poll_if_pending = deep_analysis_workflow.prepare_openclaw_poll_if_pending
_retry_deep_analysis_record = deep_analysis_workflow.retry_deep_analysis_record
_run_openclaw_deep_analysis = deep_analysis_workflow.run_openclaw_deep_analysis


@deep_analysis_router.post(
    "/deep-analyze/{webhook_id}",
    response_model=None,
    dependencies=[Depends(verify_admin_write)],
)
async def deep_analyze_webhook(
    webhook_id: int, payload: dict[str, Any] | None = None, session: AsyncSession = Depends(get_db_session)
) -> JSONResponse | JSONDict:
    payload = payload or {}
    logger.info(
        "[DeepAnalysis] Manual analysis request webhook_id=%s engine=%s", webhook_id, payload.get("engine", "auto")
    )
    event = await session.get(WebhookEvent, webhook_id)
    if not event:
        logger.warning("[DeepAnalysis] Manual analysis failed, event does not exist webhook_id=%s", webhook_id)
        return JSONResponse(status_code=404, content={"success": False, "error": "Webhook not found"})

    ctx = await _build_deep_analysis_context(event)
    requested_engine = str(payload.get("engine", "auto")).strip().lower()
    if not _is_supported_deep_analysis_engine(requested_engine):
        logger.warning(
            "[DeepAnalysis] Unsupported analysis engine webhook_id=%s engine=%s", webhook_id, requested_engine
        )
        return JSONResponse(status_code=400, content={"success": False, "error": "Unsupported engine"})
    if not OpenClawTriggerPolicy.from_config().enabled:
        logger.warning("[DeepAnalysis] OpenClaw not enabled webhook_id=%s", webhook_id)
        return JSONResponse(status_code=503, content={"success": False, "error": "No engine available"})

    try:
        res, engine_name = await _run_openclaw_deep_analysis(
            ctx, event.headers or {}, str(payload.get("user_question", ""))
        )
    except deep_analysis_workflow.DeepAnalysisExecutionError as e:
        logger.error(
            "[DeepAnalysis] Manual analysis trigger failed webhook_id=%s error=%s", webhook_id, e, exc_info=True
        )
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
        "[DeepAnalysis] Manual analysis record created analysis_id=%s webhook_id=%s status=%s engine=%s poll_delay=%s",
        analysis_id,
        webhook_id,
        record.status,
        engine_name,
        poll_delay,
    )
    return {"success": True, "data": record_data}


@deep_analysis_router.get(
    "/deep-analyses",
    response_model=DeepAnalysisListResponse,
    dependencies=[Depends(verify_api_key)],
)
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


@deep_analysis_router.get(
    "/deep-analyses/detail/{analysis_id}", response_model=None, dependencies=[Depends(verify_api_key)]
)
async def get_deep_analysis_detail(
    analysis_id: int, session: AsyncSession = Depends(get_db_session)
) -> JSONResponse | JSONDict:
    """Full record for one analysis (normalized_report + raw analysis_result).

    The list endpoint returns lightweight summaries; the dashboard calls this on
    demand when a row is expanded so heavy fields are not shipped per page.
    """
    record = await session.get(DeepAnalysis, analysis_id)
    if record is None:
        return JSONResponse(status_code=404, content={"success": False, "error": "Analysis not found"})
    data = deep_analysis_to_dict(record)
    event = await session.get(WebhookEvent, record.webhook_event_id)
    data["source"] = event.source if event else None
    data["is_duplicate"] = event.is_duplicate if event else False
    return {"success": True, "data": data}


@deep_analysis_router.get("/deep-analyses/{webhook_id}", dependencies=[Depends(verify_api_key)])
async def get_deep_analyses(
    webhook_id: int,
    limit: int = Query(50, ge=1, le=MAX_PAGE),
    session: AsyncSession = Depends(get_db_session),
) -> JSONDict:
    records = await get_deep_analyses_for_webhook(session, webhook_id, limit=limit)
    return {"success": True, "data": [deep_analysis_to_dict(record) for record in records]}


@deep_analysis_router.post(
    "/deep-analyses/{analysis_id}/retry",
    response_model=None,
    dependencies=[Depends(verify_admin_write)],
)
async def retry_deep_analysis(
    analysis_id: int, session: AsyncSession = Depends(get_db_session)
) -> JSONResponse | JSONDict:
    """Re-fetch or re-trigger the OpenClaw deep analysis result."""
    try:
        outcome = await _retry_deep_analysis_record(session, analysis_id)
    except deep_analysis_workflow.DeepAnalysisExecutionError as e:
        logger.error("[DeepAnalysis] Retry trigger failed analysis_id=%s error=%s", analysis_id, e, exc_info=True)
        return internal_error_response()
    except deep_analysis_workflow.DeepAnalysisWorkflowError as e:
        return JSONResponse(status_code=e.status_code, content={"success": False, "error": e.message})

    response: JSONDict = {"success": True, "message": outcome.message}
    if outcome.record is not None:
        response["data"] = deep_analysis_to_dict(outcome.record)
    return response


@deep_analysis_router.post(
    "/deep-analyses/{analysis_id}/forward",
    response_model=None,
    dependencies=[Depends(verify_admin_write)],
)
async def forward_deep_analysis(
    analysis_id: int,
    payload: dict[str, Any] | None = None,
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse | JSONDict:
    """Forward the deep analysis result to a given URL (Feishu card or generic Webhook)."""
    payload = payload or {}
    target_url = (payload.get("target_url") or "").strip()
    try:
        outcome = await _forward_deep_analysis_record(session, analysis_id, target_url)
    except UnsafeTargetUrlError as e:
        logger.warning("[DeepAnalysis] Manual forward target URL rejected analysis_id=%s error=%s", analysis_id, e)
        return JSONResponse(status_code=400, content={"success": False, "error": TARGET_URL_UNAVAILABLE_MESSAGE})
    except deep_analysis_workflow.DeepAnalysisWorkflowError as e:
        error = DELIVERY_ERROR_MESSAGE if e.message == "Forward was not delivered" else e.message
        return JSONResponse(status_code=e.status_code, content={"success": False, "error": error})
    except deep_analysis_workflow.DeepAnalysisDeliveryError as e:
        logger.error(
            "[DeepAnalysis] Deep analysis forward enqueue failed analysis_id=%s target=%s error=%s",
            analysis_id,
            mask_url(target_url),
            e,
        )
        return internal_error_response()
    return {"success": True, "message": "Forward enqueued", "outbox_id": outcome.outbox_id}

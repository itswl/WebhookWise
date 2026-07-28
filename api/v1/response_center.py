"""Read-only incident response work queue and knowledge-gap APIs."""

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from api import fail_response, internal_error_response, ok_response
from core.logger import get_logger
from db.session import get_db_session
from services.incidents.response_center import (
    WorkQueueBucket,
    get_knowledge_gaps,
    get_response_work_queue,
)

logger = get_logger("api.v1.response_center")

response_center_router = APIRouter(prefix="/response-center")

_RESPONSE_CENTER_ERRORS = (OSError, RuntimeError, SQLAlchemyError, TimeoutError)


@response_center_router.get("/work-queue")
async def get_work_queue_endpoint(
    bucket: WorkQueueBucket = Query("active"),
    actor: str = Query("", max_length=100),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0, le=100_000),
    sla_risk_minutes: int = Query(120, ge=5, le=24 * 60),
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """Return one bounded incident-response queue with explainable priority."""
    if bucket == "my" and not actor.strip():
        return fail_response("actor is required when bucket is 'my'", 422)
    try:
        data = await get_response_work_queue(
            session,
            bucket=bucket,
            actor=actor,
            limit=limit,
            offset=offset,
            sla_risk_minutes=sla_risk_minutes,
        )
        return ok_response(http_status=200, data=data)
    except ValueError as error:
        return fail_response(str(error), 422)
    except _RESPONSE_CENTER_ERRORS as error:
        logger.error("Failed to build response work queue: %s", error, exc_info=True)
        return internal_error_response()


@response_center_router.get("/knowledge-gaps")
async def get_knowledge_gaps_endpoint(
    window_days: int = Query(90, ge=7, le=365),
    limit: int = Query(50, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """Return recurring or costly incident patterns without a proven runbook."""
    try:
        data = await get_knowledge_gaps(
            session,
            window_days=window_days,
            limit=limit,
        )
        return ok_response(http_status=200, data=data)
    except _RESPONSE_CENTER_ERRORS as error:
        logger.error("Failed to build knowledge gaps: %s", error, exc_info=True)
        return internal_error_response()

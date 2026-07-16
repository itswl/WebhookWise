"""Audit log + handoff summary + sparkline endpoints."""

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from api import internal_error_response, ok_response
from core.auth import verify_api_key
from core.datetime_utils import utcnow
from core.logger import get_logger
from core.webhook_security import check_admin_rate_limit_dep
from db.session import get_db_session
from models import AuditLog, WebhookEvent
from services.operations.handoff import get_handoff_summary

logger = get_logger("api.v1.activity")

activity_router = APIRouter()
_ACTIVITY_ERRORS = (OSError, RuntimeError, SQLAlchemyError, ValueError, TimeoutError)


@activity_router.get(
    "/queue-health",
    dependencies=[Depends(check_admin_rate_limit_dep), Depends(verify_api_key)],
)
async def queue_health_endpoint() -> JSONResponse:
    """Redis Stream backlog health: depth vs MAXLEN, pending, and consumer lag.

    Backs the dashboard queue tile. Fails soft — an unreadable metric comes back
    as null rather than erroring the panel.
    """
    from services.operations.feature_adoption import record_feature_use
    from services.operations.queue_health import get_queue_health

    try:
        await record_feature_use("view:queue_health")
        return ok_response(http_status=200, data=await get_queue_health())
    except _ACTIVITY_ERRORS as e:
        logger.error("Failed to read queue health: %s", e, exc_info=True)
        return internal_error_response()


@activity_router.get(
    "/action-center",
    dependencies=[Depends(check_admin_rate_limit_dep), Depends(verify_api_key)],
)
async def action_center_endpoint(session: AsyncSession = Depends(get_db_session)) -> JSONResponse:
    """Current delivery, processing, and AI problems that need operator action."""
    from services.operations.action_center import get_action_center

    try:
        return ok_response(http_status=200, data=await get_action_center(session))
    except _ACTIVITY_ERRORS as e:
        logger.error("Failed to build action center: %s", e, exc_info=True)
        return internal_error_response()


@activity_router.get(
    "/audit-log",
    dependencies=[Depends(check_admin_rate_limit_dep), Depends(verify_api_key)],
)
async def list_audit_log_endpoint(
    resource_type: str = Query(""),
    page_size: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """Recent team activity: who changed what silences/rules/incidents."""
    try:
        query = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(page_size)
        if resource_type:
            query = query.where(AuditLog.resource_type == resource_type)
        rows = (await session.execute(query)).scalars().all()
        data = [
            {
                "id": r.id,
                "resource_type": r.resource_type,
                "resource_name": r.resource_name,
                "action": r.action,
                "summary": r.summary,
                "actor": r.actor,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
        return ok_response(http_status=200, data=data)
    except _ACTIVITY_ERRORS as e:
        logger.error("Failed to list audit log: %s", e, exc_info=True)
        return internal_error_response()


@activity_router.get(
    "/handoff",
    dependencies=[Depends(check_admin_rate_limit_dep), Depends(verify_api_key)],
)
async def handoff_summary_endpoint(
    hours: int = Query(8, ge=1, le=72),
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """On-call handoff summary for the last N hours."""
    try:
        data = await get_handoff_summary(session, hours=hours)
        return ok_response(http_status=200, data=data)
    except _ACTIVITY_ERRORS as e:
        logger.error("Failed to generate handoff summary: %s", e, exc_info=True)
        return internal_error_response()


@activity_router.get(
    "/sparkline",
    dependencies=[Depends(check_admin_rate_limit_dep), Depends(verify_api_key)],
)
async def sparkline_endpoint(
    days: int = Query(7, ge=1, le=60),
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """Daily alert counts for sparkline charts on the Overview page."""
    try:
        start = utcnow() - __import__("datetime").timedelta(days=days)
        rows = (
            await session.execute(
                select(
                    func.date(WebhookEvent.timestamp).label("day"),
                    func.count(WebhookEvent.id).label("cnt"),
                )
                .where(WebhookEvent.timestamp >= start)
                .group_by("day")
                .order_by("day")
            )
        ).all()
        data = [{"day": str(row[0]), "count": int(row[1])} for row in rows]
        return ok_response(http_status=200, data=data)
    except _ACTIVITY_ERRORS as e:
        logger.error("Failed to generate sparkline: %s", e, exc_info=True)
        return internal_error_response()


@activity_router.get(
    "/cross-source",
    dependencies=[Depends(check_admin_rate_limit_dep), Depends(verify_api_key)],
)
async def cross_source_endpoint(
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """Cross-source alert correlation — time windows where multiple sources fired together."""
    try:
        from services.analysis.cross_source import find_cross_source_spikes

        data = await find_cross_source_spikes(session)
        return ok_response(http_status=200, data=data)
    except _ACTIVITY_ERRORS as e:
        logger.error("Failed to run cross-source correlation: %s", e, exc_info=True)
        return internal_error_response()

"""Incident read-side API — list, detail, and summary."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from api import fail_response, internal_error_response, ok_response
from core.auth import verify_admin_write, verify_api_key
from core.datetime_utils import utcnow
from core.logger import get_logger
from core.webhook_security import check_admin_rate_limit_dep
from db.session import get_db_session
from services.incidents.queries import (
    get_incident_detail,
    get_incident_summary,
    list_incidents,
)

logger = get_logger("api.v1.incidents")

incidents_router = APIRouter()

_INCIDENT_ERRORS = (OSError, RuntimeError, SQLAlchemyError, TimeoutError, ValueError)


@incidents_router.get(
    "/incidents",
    dependencies=[Depends(check_admin_rate_limit_dep), Depends(verify_api_key)],
)
async def list_incidents_endpoint(
    cursor: int | None = Query(None),
    status: str = Query(""),
    page: int = Query(1, ge=1, le=1000),
    page_size: int = Query(30, ge=1, le=200),
    min_alert_count: Annotated[int, Query(ge=1, le=200)] = 2,
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """List incidents, newest first. Filter by status (active/quiet/closed)."""
    try:
        rows, has_more, next_cursor = await list_incidents(
            session,
            cursor=cursor,
            status=status,
            page=page,
            page_size=page_size,
            min_alert_count=min_alert_count,
        )
        return ok_response(
            data=rows,
            http_status=200,
            pagination={
                "next_cursor": next_cursor,
                "has_more": has_more,
                "page_size": page_size,
            },
        )
    except _INCIDENT_ERRORS as e:
        logger.error("Failed to list incidents: %s", e, exc_info=True)
        return internal_error_response()


@incidents_router.get(
    "/incidents/{incident_id}",
    dependencies=[Depends(check_admin_rate_limit_dep), Depends(verify_api_key)],
)
async def get_incident_detail_endpoint(
    incident_id: int, session: AsyncSession = Depends(get_db_session)
) -> JSONResponse:
    """Full incident detail with member alert timeline."""
    try:
        detail = await get_incident_detail(session, incident_id)
        if detail is None:
            return fail_response(f"Incident {incident_id} not found", 404)
        return ok_response(http_status=200, data=detail)
    except _INCIDENT_ERRORS as e:
        logger.error("Failed to get incident detail id=%s: %s", incident_id, e, exc_info=True)
        return internal_error_response()


@incidents_router.get(
    "/incidents/{incident_id}/summary",
    dependencies=[Depends(check_admin_rate_limit_dep), Depends(verify_api_key)],
)
async def get_incident_summary_endpoint(
    incident_id: int, session: AsyncSession = Depends(get_db_session)
) -> JSONResponse:
    """Return the structured summary of an incident (including LLM analysis)."""
    try:
        data = await get_incident_summary(session, incident_id)
        if data is None:
            return fail_response(f"Incident {incident_id} not found", 404)
        return ok_response(http_status=200, data=data)
    except _INCIDENT_ERRORS as e:
        logger.error("Failed to get incident summary id=%s: %s", incident_id, e, exc_info=True)
        return internal_error_response()


@incidents_router.post(
    "/incidents/{incident_id}/summarize",
    dependencies=[Depends(check_admin_rate_limit_dep), Depends(verify_admin_write)],
)
async def trigger_incident_summary_endpoint(incident_id: int) -> JSONResponse:
    """Manually trigger LLM summarization for a specific incident."""
    from services.incidents.summary import summarize_incident

    try:
        result = await summarize_incident(incident_id)
        if result is None:
            return fail_response("Incident not found, has no members, or AI is unavailable", 409)
        return ok_response(http_status=200, data=result)
    except _INCIDENT_ERRORS as e:
        logger.error("Failed to summarize incident id=%s: %s", incident_id, e, exc_info=True)
        return internal_error_response()


@incidents_router.post(
    "/incidents/{incident_id}/close",
    response_model=None,
    dependencies=[Depends(verify_admin_write)],
)
async def close_incident_endpoint(incident_id: int, session: AsyncSession = Depends(get_db_session)) -> JSONResponse:
    """Mark an incident as closed (operator resolution).

    A closed incident no longer appears in the active list but is preserved
    for historical review. Re-opening is a separate call so closure is always
    an explicit operator action, not an automated side effect.
    """
    from models import Incident
    from services.operations.audit_logger import add_audit

    try:
        incident = await session.get(Incident, incident_id)
        if incident is None:
            return fail_response(f"Incident {incident_id} not found", 404)
        incident.status = "closed"
        incident.workflow_status = "resolved"
        incident.resolved_at = utcnow()
        incident.ended_at = incident.ended_at or utcnow()
        if incident.summary_analysis is None and incident.alert_count >= 2:
            incident.summary_status = "pending"
            incident.summary_attempts = 0
            incident.summary_next_attempt_at = utcnow()
            incident.summary_last_error = None
        elif incident.summary_analysis is None:
            incident.summary_status = "skipped"
            incident.summary_next_attempt_at = None
            incident.summary_last_error = "singleton incidents are not summarized"
        add_audit(
            session,
            "incident",
            incident_id,
            incident.title,
            "closed",
            f"Incident closed: {incident.title}",
        )
        await session.commit()
        logger.info("[Incidents] Marked incident id=%s as closed", incident_id)
        return ok_response(http_status=200, message="incident closed", data={"id": incident_id, "status": "closed"})
    except _INCIDENT_ERRORS as e:
        logger.error("Failed to close incident id=%s: %s", incident_id, e, exc_info=True)
        return internal_error_response()


@incidents_router.post(
    "/incidents/{incident_id}/reopen",
    response_model=None,
    dependencies=[Depends(verify_admin_write)],
)
async def reopen_incident_endpoint(incident_id: int, session: AsyncSession = Depends(get_db_session)) -> JSONResponse:
    """Re-open a previously closed or quieted incident."""
    from models import Incident
    from services.operations.audit_logger import add_audit

    try:
        incident = await session.get(Incident, incident_id)
        if incident is None:
            return fail_response(f"Incident {incident_id} not found", 404)
        incident.status = "active"
        incident.workflow_status = "open"
        incident.resolved_at = None
        incident.ended_at = None
        if incident.summary_analysis is None:
            incident.summary_status = None
            incident.summary_attempts = 0
            incident.summary_next_attempt_at = None
            incident.summary_last_error = None
        add_audit(
            session,
            "incident",
            incident_id,
            incident.title,
            "reopened",
            f"Incident reopened: {incident.title}",
        )
        await session.commit()
        logger.info("[Incidents] Re-opened incident id=%s", incident_id)
        return ok_response(http_status=200, message="incident re-opened", data={"id": incident_id, "status": "active"})
    except _INCIDENT_ERRORS as e:
        logger.error("Failed to reopen incident id=%s: %s", incident_id, e, exc_info=True)
        return internal_error_response()

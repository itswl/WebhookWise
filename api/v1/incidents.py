"""Incident read-side API — list, detail, and summary."""

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from api import fail_response, internal_error_response, ok_response
from core.auth import verify_api_key
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


def _log_audit(
    resource_type: str, resource_id: int | None, resource_name: str | None, action: str, summary: str
) -> None:
    """Fire-and-forget audit log entry."""
    try:
        import asyncio

        asyncio.ensure_future(__record_audit(resource_type, resource_id, resource_name, action, summary))
    except RuntimeError:
        pass


async def __record_audit(
    resource_type: str, resource_id: int | None, resource_name: str | None, action: str, summary: str
) -> None:
    from db.session import session_scope
    from models import AuditLog

    try:
        async with session_scope() as session:
            session.add(
                AuditLog(
                    resource_type=resource_type,
                    resource_id=resource_id,
                    resource_name=resource_name,
                    action=action,
                    summary=summary[:500],
                    actor="dashboard",
                )
            )
    except Exception:
        pass


@incidents_router.get(
    "/incidents",
    dependencies=[Depends(check_admin_rate_limit_dep), Depends(verify_api_key)],
)
async def list_incidents_endpoint(
    cursor: int | None = Query(None),
    status: str = Query(""),
    page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=1, le=200),
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """List incidents, newest first. Filter by status (active/quiet/closed)."""
    try:
        rows, has_more, next_cursor = await list_incidents(
            session, cursor=cursor, status=status, page=page, page_size=page_size
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
    dependencies=[Depends(check_admin_rate_limit_dep), Depends(verify_api_key)],
)
async def trigger_incident_summary_endpoint(
    incident_id: int, session: AsyncSession = Depends(get_db_session)
) -> JSONResponse:
    """Manually trigger LLM summarization for a specific incident."""
    from services.incidents.summary import summarize_incident

    try:
        result = await summarize_incident(session, incident_id)
        return ok_response(http_status=200, data=result)
    except _INCIDENT_ERRORS as e:
        logger.error("Failed to summarize incident id=%s: %s", incident_id, e, exc_info=True)
        return internal_error_response()


@incidents_router.post(
    "/incidents/{incident_id}/close",
    response_model=None,
    dependencies=[Depends(verify_api_key)],
)
async def close_incident_endpoint(
    incident_id: int, session: AsyncSession = Depends(get_db_session)
) -> JSONResponse:
    """Mark an incident as closed (operator resolution).

    A closed incident no longer appears in the active list but is preserved
    for historical review. Re-opening is a separate call so closure is always
    an explicit operator action, not an automated side effect.
    """
    from models import Incident

    try:
        incident = await session.get(Incident, incident_id)
        if incident is None:
            return fail_response(f"Incident {incident_id} not found", 404)
        incident.status = "closed"
        await session.commit()
        _log_audit("incident", incident_id, incident.title, "closed", f"Incident closed: {incident.title}")
        logger.info("[Incidents] Marked incident id=%s as closed", incident_id)
        return ok_response(http_status=200, message="incident closed", data={"id": incident_id, "status": "closed"})
    except _INCIDENT_ERRORS as e:
        logger.error("Failed to close incident id=%s: %s", incident_id, e, exc_info=True)
        return internal_error_response()


@incidents_router.post(
    "/incidents/{incident_id}/reopen",
    response_model=None,
    dependencies=[Depends(verify_api_key)],
)
async def reopen_incident_endpoint(
    incident_id: int, session: AsyncSession = Depends(get_db_session)
) -> JSONResponse:
    """Re-open a previously closed or quieted incident."""
    from models import Incident

    try:
        incident = await session.get(Incident, incident_id)
        if incident is None:
            return fail_response(f"Incident {incident_id} not found", 404)
        incident.status = "active"
        await session.commit()
        _log_audit("incident", incident_id, incident.title, "reopened", f"Incident reopened: {incident.title}")
        logger.info("[Incidents] Re-opened incident id=%s", incident_id)
        return ok_response(http_status=200, message="incident re-opened", data={"id": incident_id, "status": "active"})
    except _INCIDENT_ERRORS as e:
        logger.error("Failed to reopen incident id=%s: %s", incident_id, e, exc_info=True)
        return internal_error_response()

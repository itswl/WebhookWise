"""Incident read-side API — list, detail, and summary."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse, Response
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from api import fail_response, internal_error_response, ok_response
from core.auth import verify_admin_write, verify_api_key
from core.datetime_utils import utcnow
from core.logger import get_logger
from core.webhook_security import check_admin_rate_limit_dep
from db.session import get_db_session
from schemas.intelligence import (
    IntelligenceFeedbackRequest,
    RunbookExecutionStartRequest,
    RunbookExecutionUpdateRequest,
)
from services.incidents.intelligence import (
    get_incident_intelligence,
    record_intelligence_feedback,
)
from services.incidents.queries import (
    get_incident_detail,
    get_incident_summary,
    list_incidents,
)
from services.incidents.runbooks import (
    RunbookExecutionConflictError,
    RunbookExecutionNotFoundError,
    list_runbook_executions,
    runbook_execution_response,
    start_runbook_execution,
    update_runbook_execution,
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
    "/incidents/{incident_id}/intelligence",
    dependencies=[Depends(check_admin_rate_limit_dep), Depends(verify_api_key)],
)
async def get_incident_intelligence_endpoint(
    incident_id: int,
    limit: int = Query(3, ge=1, le=5),
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """Return explainable similar incidents, related changes, and runbooks."""
    try:
        data = await get_incident_intelligence(session, incident_id, limit=limit)
        if data is None:
            return fail_response(f"Incident {incident_id} not found", 404)
        return ok_response(http_status=200, data=data)
    except _INCIDENT_ERRORS as e:
        logger.error("Failed to get incident intelligence id=%s: %s", incident_id, e, exc_info=True)
        return internal_error_response()


@incidents_router.post(
    "/incidents/{incident_id}/intelligence/feedback",
    dependencies=[Depends(check_admin_rate_limit_dep), Depends(verify_admin_write)],
)
async def record_incident_intelligence_feedback_endpoint(
    incident_id: int,
    request: IntelligenceFeedbackRequest,
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """Record operator feedback for one incident-intelligence recommendation."""
    try:
        feedback = await record_intelligence_feedback(
            session,
            incident_id,
            request.model_dump(),
        )
        if feedback is None:
            return fail_response(f"Incident {incident_id} not found", 404)
        return ok_response(
            http_status=200,
            message="incident intelligence feedback recorded",
            data={
                "incident_id": incident_id,
                "recommendation_type": feedback.recommendation_type,
                "candidate_ref": feedback.candidate_ref,
                "verdict": feedback.verdict,
            },
        )
    except _INCIDENT_ERRORS as e:
        logger.error("Failed to record incident intelligence feedback id=%s: %s", incident_id, e, exc_info=True)
        return internal_error_response()


@incidents_router.get(
    "/incidents/{incident_id}/runbook-executions",
    dependencies=[Depends(check_admin_rate_limit_dep), Depends(verify_api_key)],
)
async def list_runbook_executions_endpoint(
    incident_id: int,
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """List manual runbook executions attached to an incident."""
    try:
        return ok_response(
            http_status=200,
            data=await list_runbook_executions(session, incident_id),
        )
    except RunbookExecutionNotFoundError as error:
        return fail_response(str(error), 404)
    except _INCIDENT_ERRORS as error:
        logger.error("Failed to list runbook executions incident_id=%s: %s", incident_id, error, exc_info=True)
        return internal_error_response()


@incidents_router.post(
    "/incidents/{incident_id}/runbook-executions",
    dependencies=[Depends(check_admin_rate_limit_dep), Depends(verify_admin_write)],
)
async def start_runbook_execution_endpoint(
    incident_id: int,
    request: RunbookExecutionStartRequest,
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """Start or retrieve one idempotent, operator-driven runbook execution."""
    try:
        execution, created = await start_runbook_execution(
            session,
            incident_id=incident_id,
            candidate_ref=request.candidate_ref,
            actor=request.actor,
        )
        return ok_response(
            http_status=201 if created else 200,
            message="runbook execution started" if created else "runbook execution already exists",
            data=runbook_execution_response(execution),
        )
    except RunbookExecutionNotFoundError as error:
        return fail_response(str(error), 404)
    except _INCIDENT_ERRORS as error:
        logger.error("Failed to start runbook execution incident_id=%s: %s", incident_id, error, exc_info=True)
        return internal_error_response()


@incidents_router.put(
    "/incidents/{incident_id}/runbook-executions/{execution_id}",
    dependencies=[Depends(check_admin_rate_limit_dep), Depends(verify_admin_write)],
)
async def update_runbook_execution_endpoint(
    incident_id: int,
    execution_id: int,
    request: RunbookExecutionUpdateRequest,
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """Update manual step progress, terminal state, notes, or effectiveness."""
    try:
        changes = request.model_dump(exclude_unset=True, exclude={"actor"})
        execution = await update_runbook_execution(
            session,
            incident_id=incident_id,
            execution_id=execution_id,
            changes=changes,
            actor=request.actor,
        )
        return ok_response(
            http_status=200,
            message="runbook execution updated",
            data=runbook_execution_response(execution),
        )
    except RunbookExecutionNotFoundError as error:
        return fail_response(str(error), 404)
    except RunbookExecutionConflictError as error:
        return fail_response(str(error), 409)
    except _INCIDENT_ERRORS as error:
        logger.error(
            "Failed to update runbook execution incident_id=%s execution_id=%s: %s",
            incident_id,
            execution_id,
            error,
            exc_info=True,
        )
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
    "/incidents/{incident_id}/postmortem",
    dependencies=[Depends(check_admin_rate_limit_dep), Depends(verify_api_key)],
)
async def export_incident_postmortem_endpoint(
    incident_id: int, session: AsyncSession = Depends(get_db_session)
) -> Response:
    """Export the incident as a Markdown postmortem draft (download)."""
    from services.incidents.postmortem import build_postmortem_markdown
    from services.operations.feature_adoption import record_feature_use

    try:
        markdown = await build_postmortem_markdown(session, incident_id)
        if markdown is None:
            return fail_response(f"Incident {incident_id} not found", 404)
        await record_feature_use("action:postmortem_exported")
        return Response(
            content=markdown,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="postmortem-incident-{incident_id}.md"'},
        )
    except _INCIDENT_ERRORS as e:
        logger.error("Failed to export postmortem id=%s: %s", incident_id, e, exc_info=True)
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

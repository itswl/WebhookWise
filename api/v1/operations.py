"""Operator workflow, notes, feedback, and incident-editing endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from api import fail_response, internal_error_response, ok_response
from core.auth import verify_admin_write
from core.logger import get_logger
from db.session import get_db_session
from schemas.operations import (
    FeedbackCreateRequest,
    IncidentMergeRequest,
    IncidentSplitRequest,
    IntegrationSetupRequest,
    IntegrationTestRequest,
    NoteCreateRequest,
    RemediationRequest,
    WorkflowUpdateRequest,
)
from services.operations.integration_catalog import install_integration, integration_catalog, test_integration
from services.operations.remediation import run_remediation
from services.operations.workflow import (
    add_feedback,
    add_note,
    feedback_summary,
    list_notes,
    merge_incidents,
    split_incident,
    update_workflow,
)

logger = get_logger("api.v1.operations")
operations_router = APIRouter()

_OPERATION_ERRORS = (OSError, RuntimeError, SQLAlchemyError, TimeoutError, TypeError, ValueError)


def _resource_type(kind: str) -> str:
    return "webhook_event" if kind == "webhooks" else "incident"


@operations_router.put(
    "/{kind}/{resource_id}/workflow",
    dependencies=[Depends(verify_admin_write)],
)
async def update_workflow_endpoint(
    kind: str,
    resource_id: int,
    payload: WorkflowUpdateRequest,
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    if kind not in {"webhooks", "incidents"}:
        return fail_response("Unsupported workflow resource", 404)
    try:
        data = await update_workflow(
            session,
            resource_type=_resource_type(kind),
            resource_id=resource_id,
            changes=payload.model_dump(exclude_unset=True),
        )
        if data is None:
            return fail_response("Workflow resource not found", 404)
        return ok_response(data=data, message="Workflow updated", http_status=200)
    except _OPERATION_ERRORS as error:
        logger.error("Failed to update workflow kind=%s id=%s: %s", kind, resource_id, error, exc_info=True)
        return internal_error_response()


@operations_router.get("/{kind}/{resource_id}/notes")
async def list_notes_endpoint(
    kind: str,
    resource_id: int,
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    if kind not in {"webhooks", "incidents"}:
        return fail_response("Unsupported note resource", 404)
    try:
        data = await list_notes(session, resource_type=_resource_type(kind), resource_id=resource_id)
        if data is None:
            return fail_response("Note resource not found", 404)
        return ok_response(data=data, http_status=200)
    except _OPERATION_ERRORS as error:
        logger.error("Failed to list notes kind=%s id=%s: %s", kind, resource_id, error, exc_info=True)
        return internal_error_response()


@operations_router.post(
    "/{kind}/{resource_id}/notes",
    dependencies=[Depends(verify_admin_write)],
)
async def add_note_endpoint(
    kind: str,
    resource_id: int,
    payload: NoteCreateRequest,
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    if kind not in {"webhooks", "incidents"}:
        return fail_response("Unsupported note resource", 404)
    try:
        data = await add_note(
            session,
            resource_type=_resource_type(kind),
            resource_id=resource_id,
            body=payload.body,
            actor=payload.actor,
        )
        if data is None:
            return fail_response("Note resource not found", 404)
        return ok_response(data=data, message="Note added", http_status=201)
    except _OPERATION_ERRORS as error:
        logger.error("Failed to add note kind=%s id=%s: %s", kind, resource_id, error, exc_info=True)
        return internal_error_response()


@operations_router.post(
    "/{kind}/{resource_id}/feedback",
    dependencies=[Depends(verify_admin_write)],
)
async def add_feedback_endpoint(
    kind: str,
    resource_id: int,
    payload: FeedbackCreateRequest,
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    if kind not in {"webhooks", "incidents"}:
        return fail_response("Unsupported feedback resource", 404)
    try:
        data = await add_feedback(
            session,
            resource_type=_resource_type(kind),
            resource_id=resource_id,
            **payload.model_dump(),
        )
        if data is None:
            return fail_response("Feedback resource not found", 404)
        return ok_response(data=data, message="Feedback recorded", http_status=201)
    except _OPERATION_ERRORS as error:
        logger.error("Failed to add feedback kind=%s id=%s: %s", kind, resource_id, error, exc_info=True)
        return internal_error_response()


@operations_router.get("/feedback/summary")
async def feedback_summary_endpoint(
    days: int = Query(30, ge=1, le=365),
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    try:
        return ok_response(data=await feedback_summary(session, days=days), http_status=200)
    except _OPERATION_ERRORS as error:
        logger.error("Failed to summarize feedback: %s", error, exc_info=True)
        return internal_error_response()


@operations_router.post(
    "/incidents/{incident_id}/merge",
    dependencies=[Depends(verify_admin_write)],
)
async def merge_incidents_endpoint(
    incident_id: int,
    payload: IncidentMergeRequest,
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    try:
        data = await merge_incidents(
            session,
            destination_id=incident_id,
            source_ids=payload.source_incident_ids,
        )
        if data is None:
            return fail_response("One or more incidents could not be merged", 409)
        return ok_response(data=data, message="Incidents merged", http_status=200)
    except _OPERATION_ERRORS as error:
        logger.error("Failed to merge incidents into id=%s: %s", incident_id, error, exc_info=True)
        return internal_error_response()


@operations_router.post(
    "/incidents/{incident_id}/split",
    dependencies=[Depends(verify_admin_write)],
)
async def split_incident_endpoint(
    incident_id: int,
    payload: IncidentSplitRequest,
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    try:
        data = await split_incident(session, source_id=incident_id, event_ids=payload.event_ids)
        if data is None:
            return fail_response("The selected alerts cannot be split from this incident", 409)
        return ok_response(data=data, message="Incident split", http_status=201)
    except _OPERATION_ERRORS as error:
        logger.error("Failed to split incident id=%s: %s", incident_id, error, exc_info=True)
        return internal_error_response()


@operations_router.post(
    "/action-center/actions",
    dependencies=[Depends(verify_admin_write)],
)
async def run_remediation_endpoint(
    payload: RemediationRequest,
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    try:
        data = await run_remediation(session, **payload.model_dump())
        status = 200 if data.get("changed") else 409
        if not data.get("changed"):
            return fail_response(str(data.get("reason") or "No eligible resource was changed"), status)
        return ok_response(data=data, message="Action completed", http_status=status)
    except _OPERATION_ERRORS as error:
        logger.error("Action Center command failed action=%s: %s", payload.action, error, exc_info=True)
        return internal_error_response()


@operations_router.get("/integrations/catalog")
async def integration_catalog_endpoint() -> JSONResponse:
    return ok_response(data=integration_catalog(), http_status=200)


@operations_router.post(
    "/integrations/test",
    dependencies=[Depends(verify_admin_write)],
)
async def test_integration_endpoint(payload: IntegrationTestRequest) -> JSONResponse:
    try:
        data = await test_integration(payload)
        if not data["healthy"]:
            return fail_response(str(data.get("message") or "Integration target test failed"), 422)
        return ok_response(data=data, message="Integration target is healthy", http_status=200)
    except _OPERATION_ERRORS as error:
        logger.warning("Integration target test failed template=%s: %s", payload.template_id, error)
        return fail_response("Integration target test failed", 422)


@operations_router.post(
    "/integrations",
    dependencies=[Depends(verify_admin_write)],
)
async def install_integration_endpoint(
    payload: IntegrationSetupRequest,
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    try:
        data = await install_integration(session, payload)
        return ok_response(data=data, message="Integration installed", http_status=201)
    except _OPERATION_ERRORS as error:
        logger.warning("Integration setup failed template=%s: %s", payload.template_id, error)
        return fail_response("Integration setup failed", 422)

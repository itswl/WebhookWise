"""Normalized change-event ingestion for incident correlation."""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from api import fail_response, internal_error_response, ok_response
from core.auth import verify_api_key, verify_change_ingest_token
from core.logger import get_logger
from core.webhook_security import check_admin_rate_limit_dep
from db.session import get_db_session
from schemas.intelligence import ChangeEventCreateRequest
from services.incidents.change_impact import get_change_impact
from services.incidents.intelligence import change_event_response, upsert_change_event

logger = get_logger("api.v1.changes")

changes_router = APIRouter()

_CHANGE_ERRORS = (OSError, RuntimeError, SQLAlchemyError, TimeoutError, ValueError)


@changes_router.get(
    "/changes/{change_id}/impact",
    dependencies=[
        Depends(check_admin_rate_limit_dep),
        Depends(verify_api_key),
    ],
)
async def get_change_impact_endpoint(
    change_id: int,
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """Return a deterministic, explainable before/after impact assessment."""
    try:
        impact = await get_change_impact(session, change_id)
        if impact is None:
            return fail_response(f"Change {change_id} not found", 404)
        return ok_response(http_status=200, data=impact)
    except _CHANGE_ERRORS as error:
        logger.error("Failed to assess change id=%s: %s", change_id, error, exc_info=True)
        return internal_error_response()


@changes_router.post(
    "/changes",
    dependencies=[
        Depends(check_admin_rate_limit_dep),
        Depends(verify_change_ingest_token),
    ],
)
async def ingest_change_event_endpoint(
    request: ChangeEventCreateRequest,
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """Idempotently ingest one deployment, configuration, or infrastructure change."""
    try:
        change, created = await upsert_change_event(
            session,
            request.model_dump(),
        )
        return ok_response(
            http_status=201 if created else 200,
            message="change event ingested" if created else "change event updated",
            data=change_event_response(change),
        )
    except _CHANGE_ERRORS as error:
        logger.error("Failed to ingest change event: %s", error, exc_info=True)
        return internal_error_response()

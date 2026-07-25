"""Normalized change-event ingestion for incident correlation."""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from api import internal_error_response, ok_response
from core.auth import verify_admin_write
from core.logger import get_logger
from db.session import get_db_session
from schemas.intelligence import ChangeEventCreateRequest
from services.incidents.intelligence import change_event_response, upsert_change_event

logger = get_logger("api.v1.changes")

changes_router = APIRouter()

_CHANGE_ERRORS = (OSError, RuntimeError, SQLAlchemyError, TimeoutError, ValueError)


@changes_router.post(
    "/changes",
    dependencies=[Depends(verify_admin_write)],
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

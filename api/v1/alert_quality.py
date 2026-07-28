"""Read-only alert-source quality API."""

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from api import internal_error_response, ok_response
from core.logger import get_logger
from db.session import get_db_session
from services.webhooks.alert_quality import get_alert_quality_overview

logger = get_logger("api.v1.alert_quality")

alert_quality_router = APIRouter(prefix="/alert-quality")

_ALERT_QUALITY_ERRORS = (OSError, RuntimeError, SQLAlchemyError, TimeoutError, TypeError, ValueError)


@alert_quality_router.get("/overview")
async def get_alert_quality_endpoint(
    window_days: int = Query(7, ge=1, le=90),
    source_limit: int = Query(100, ge=1, le=200),
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """Return bounded quality diagnostics without changing alert sources."""
    try:
        data = await get_alert_quality_overview(
            session,
            window_days=window_days,
            source_limit=source_limit,
        )
        return ok_response(data=data)
    except _ALERT_QUALITY_ERRORS as error:
        logger.error("Failed to build alert quality overview: %s", error, exc_info=True)
        return internal_error_response()


__all__ = ["alert_quality_router"]

"""Discovered service profiles derived from incident and change history."""

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from api import fail_response, internal_error_response, ok_response
from core.logger import get_logger
from db.session import get_db_session
from services.incidents.service_profiles import get_service_profile, list_service_profiles

logger = get_logger("api.v1.services")

services_router = APIRouter()

_SERVICE_ERRORS = (OSError, RuntimeError, SQLAlchemyError, TimeoutError, ValueError)


@services_router.get("/services")
async def list_service_profiles_endpoint(
    window_days: int = Query(30, ge=1, le=365),
    limit: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """List services discovered from recent non-empty incidents."""
    try:
        rows = await list_service_profiles(session, window_days=window_days, limit=limit + 1)
        has_more = len(rows) > limit
        return ok_response(
            http_status=200,
            data=rows[:limit],
            pagination={
                "has_more": has_more,
                "next_cursor": None,
                "page_size": limit,
            },
        )
    except _SERVICE_ERRORS as error:
        logger.error("Failed to list service profiles: %s", error, exc_info=True)
        return internal_error_response()


@services_router.get("/service-profile")
async def get_service_profile_endpoint(
    service: str = Query(..., min_length=1, max_length=300),
    environment: str = Query("", max_length=200),
    window_days: int = Query(30, ge=1, le=365),
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """Return one transparent service profile without requiring a CMDB."""
    try:
        profile = await get_service_profile(
            session,
            service,
            environment=environment,
            window_days=window_days,
        )
        if profile is None:
            return fail_response(f"Service profile {service!r} not found", 404)
        return ok_response(http_status=200, data=profile)
    except _SERVICE_ERRORS as error:
        logger.error("Failed to get service profile: %s", error, exc_info=True)
        return internal_error_response()

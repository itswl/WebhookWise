"""Silence (manual mute / snooze) API routes."""

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth import verify_admin_write, verify_api_key
from core.datetime_utils import naive_utc, utcnow
from core.logger import get_logger
from db.session import get_db_session
from schemas.silences import (
    SilenceCreateRequest,
    SilenceDetailResponse,
    SilenceListResponse,
    SilenceUpdateRequest,
    silence_to_dict,
)
from services.silences.store import (
    create_silence,
    delete_silence,
    get_silence,
    lift_silence,
    list_silences,
    update_silence,
)

logger = get_logger("api.v1.silences")

silences_router = APIRouter()

JSONDict = dict[str, object]


@silences_router.get(
    "/silences",
    response_model=SilenceListResponse,
    dependencies=[Depends(verify_api_key)],
)
async def list_silences_endpoint(
    active_only: bool = Query(False),
    session: AsyncSession = Depends(get_db_session),
) -> JSONDict:
    now = utcnow()
    silences = await list_silences(session, active_only=active_only)
    return {"success": True, "data": [silence_to_dict(s, now=now) for s in silences]}


@silences_router.post(
    "/silences",
    response_model=SilenceDetailResponse,
    dependencies=[Depends(verify_admin_write)],
)
async def create_silence_endpoint(
    payload: SilenceCreateRequest, session: AsyncSession = Depends(get_db_session)
) -> JSONDict:
    data = payload.to_service_kwargs()
    expires_at = data["expires_at"]
    silence = await create_silence(
        session=session,
        match_source=data["match_source"],
        match_importance=data["match_importance"],
        match_event_type=data["match_event_type"],
        match_project=data["match_project"],
        match_region=data["match_region"],
        match_environment=data["match_environment"],
        match_payload=data["match_payload"],
        comment=data["comment"],
        created_by=data["created_by"],
        expires_at=naive_utc(expires_at) if expires_at is not None else None,
    )
    await session.commit()
    logger.info(
        "[SilenceAPI] Silence created silence_id=%s source=%s importance=%s expires_at=%s",
        silence.id,
        silence.match_source,
        silence.match_importance,
        silence.expires_at,
    )
    return {"success": True, "data": silence_to_dict(silence, now=utcnow()), "message": "Silence created"}


@silences_router.put(
    "/silences/{silence_id}",
    response_model=SilenceDetailResponse,
    dependencies=[Depends(verify_admin_write)],
)
async def update_silence_endpoint(
    silence_id: int, payload: SilenceUpdateRequest, session: AsyncSession = Depends(get_db_session)
) -> JSONDict | JSONResponse:
    data = payload.to_update_payload()
    if "expires_at" in data and data["expires_at"] is not None:
        data["expires_at"] = naive_utc(data["expires_at"])
    silence = await update_silence(session=session, silence_id=silence_id, payload=data)
    if silence is None:
        return JSONResponse(status_code=404, content={"success": False, "error": "Silence does not exist"})
    await session.commit()
    logger.info("[SilenceAPI] Silence updated silence_id=%s", silence_id)
    return {"success": True, "data": silence_to_dict(silence, now=utcnow()), "message": "Silence updated"}


@silences_router.post(
    "/silences/{silence_id}/lift",
    response_model=SilenceDetailResponse,
    dependencies=[Depends(verify_admin_write)],
)
async def lift_silence_endpoint(
    silence_id: int, session: AsyncSession = Depends(get_db_session)
) -> JSONDict | JSONResponse:
    silence = await lift_silence(session=session, silence_id=silence_id)
    if silence is None:
        return JSONResponse(status_code=404, content={"success": False, "error": "Silence does not exist"})
    await session.commit()
    logger.info("[SilenceAPI] Silence lifted silence_id=%s", silence_id)
    return {"success": True, "data": silence_to_dict(silence, now=utcnow()), "message": "Silence lifted"}


@silences_router.delete(
    "/silences/{silence_id}",
    response_model=None,
    dependencies=[Depends(verify_admin_write)],
)
async def delete_silence_endpoint(
    silence_id: int, session: AsyncSession = Depends(get_db_session)
) -> JSONDict | JSONResponse:
    existing = await get_silence(session, silence_id)
    if not existing:
        return JSONResponse(status_code=404, content={"success": False, "error": "Silence does not exist"})
    await delete_silence(session=session, silence_id=silence_id)
    await session.commit()
    logger.info("[SilenceAPI] Silence deleted silence_id=%s", silence_id)
    return {"success": True, "message": "Silence deleted"}

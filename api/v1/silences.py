"""Silence (manual mute / snooze) API routes."""

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth import verify_admin_write, verify_api_key
from core.datetime_utils import naive_utc, utcnow
from core.logger import get_logger
from db.session import get_db_session
from schemas.silences import (
    MaintenanceWindowCreateRequest,
    MaintenanceWindowDetailResponse,
    MaintenanceWindowListResponse,
    MaintenanceWindowUpdateRequest,
    SilenceBacktestRequest,
    SilenceBacktestResponse,
    SilenceCreateRequest,
    SilenceDebtResponse,
    SilenceDetailResponse,
    SilenceListResponse,
    SilenceUpdateRequest,
    maintenance_window_to_dict,
    silence_to_dict,
)
from services.operations.audit_logger import add_audit
from services.silences.backtest import backtest_silence_rule
from services.silences.store import (
    create_silence,
    delete_silence,
    get_silence,
    lift_silence,
    list_silences,
    update_silence,
)
from services.webhooks.decision_trace_queries import get_silence_suppression_counts

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
    # Annotate each silence with how many alerts it has suppressed (its ROI):
    # a zero count on an active rule is a "zombie" silence worth reviewing.
    suppression = await get_silence_suppression_counts(session, silence_ids=[s.id for s in silences])
    data = []
    for s in silences:
        item = silence_to_dict(s, now=now)
        stat = suppression.get(s.id)
        item["suppressed_count"] = stat["count"] if stat else 0
        item["last_suppressed_at"] = stat["last_suppressed_at"] if stat else None
        data.append(item)
    return {"success": True, "data": data}


@silences_router.get(
    "/silences/debt",
    response_model=SilenceDebtResponse,
    dependencies=[Depends(verify_api_key)],
)
async def silence_debt_endpoint(
    window_days: int = Query(30, ge=1, le=90),
    session: AsyncSession = Depends(get_db_session),
) -> JSONDict:
    """Rank active silences by how much they have suppressed over the window.

    Registered before ``/silences/{silence_id}`` so the static path wins; the
    detail route's int converter would reject "debt" anyway.
    """
    from services.operations.feature_adoption import record_feature_use
    from services.operations.silence_debt import get_silence_debt

    await record_feature_use("view:silence_debt")
    data = await get_silence_debt(session, window_days=window_days)
    return {"success": True, "data": data}


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
    add_audit(
        session,
        "silence",
        silence.id,
        silence.comment,
        "created",
        f"Silence created: {silence.comment or silence.id}",
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
    add_audit(
        session,
        "silence",
        silence.id,
        silence.comment,
        "updated",
        f"Silence updated: {silence.comment or silence.id}",
    )
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
    add_audit(
        session,
        "silence",
        silence.id,
        silence.comment,
        "lifted",
        f"Silence lifted: {silence.comment or silence.id}",
    )
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
    add_audit(
        session,
        "silence",
        silence_id,
        existing.comment,
        "deleted",
        f"Silence deleted: {existing.comment or silence_id}",
    )
    await session.commit()
    logger.info("[SilenceAPI] Silence deleted silence_id=%s", silence_id)
    return {"success": True, "message": "Silence deleted"}


@silences_router.get(
    "/maintenance-windows",
    response_model=MaintenanceWindowListResponse,
    dependencies=[Depends(verify_api_key)],
)
async def list_maintenance_windows_endpoint(
    session: AsyncSession = Depends(get_db_session),
) -> JSONDict:
    from sqlalchemy import select

    from models import MaintenanceWindow
    from services.silences.maintenance_windows import active_occurrence

    now = utcnow()
    windows = (await session.execute(select(MaintenanceWindow).order_by(MaintenanceWindow.name))).scalars().all()
    data = []
    for window in windows:
        try:
            active = window.enabled and active_occurrence(window, now) is not None
        except ValueError:
            active = False
        data.append(maintenance_window_to_dict(window, active_now=active))
    return {"success": True, "data": data}


@silences_router.post(
    "/maintenance-windows",
    response_model=MaintenanceWindowDetailResponse,
    dependencies=[Depends(verify_admin_write)],
)
async def create_maintenance_window_endpoint(
    payload: MaintenanceWindowCreateRequest, session: AsyncSession = Depends(get_db_session)
) -> JSONDict | JSONResponse:
    from sqlalchemy import select

    from models import MaintenanceWindow
    from services.silences.maintenance_windows import active_occurrence, sweep_maintenance_windows

    exists = (
        await session.execute(select(MaintenanceWindow.id).where(MaintenanceWindow.name == payload.name))
    ).scalar_one_or_none()
    if exists is not None:
        return JSONResponse(
            status_code=409,
            content={"success": False, "error": f"A maintenance window named {payload.name!r} already exists"},
        )
    window = MaintenanceWindow(**payload.to_model_kwargs())
    session.add(window)
    await session.flush()
    # Materialize immediately so a window covering "now" mutes without waiting
    # for the next scheduler tick.
    await sweep_maintenance_windows(session)
    add_audit(
        session,
        "maintenance_window",
        window.id,
        window.name,
        "created",
        f"Maintenance window created: {window.name}",
    )
    await session.commit()
    logger.info("[SilenceAPI] Maintenance window created id=%s name=%s", window.id, window.name)
    from services.operations.feature_adoption import record_feature_use

    await record_feature_use("action:maintenance_window_created")
    active = window.enabled and active_occurrence(window, utcnow()) is not None
    return {
        "success": True,
        "data": maintenance_window_to_dict(window, active_now=active),
        "message": "Maintenance window created",
    }


@silences_router.put(
    "/maintenance-windows/{window_id}",
    response_model=MaintenanceWindowDetailResponse,
    dependencies=[Depends(verify_admin_write)],
)
async def update_maintenance_window_endpoint(
    window_id: int, payload: MaintenanceWindowUpdateRequest, session: AsyncSession = Depends(get_db_session)
) -> JSONDict | JSONResponse:
    from sqlalchemy import select

    from models import MaintenanceWindow
    from services.silences.maintenance_windows import active_occurrence, sweep_maintenance_windows

    window = await session.get(MaintenanceWindow, window_id)
    if window is None:
        return JSONResponse(status_code=404, content={"success": False, "error": "Maintenance window does not exist"})
    name_clash = (
        await session.execute(
            select(MaintenanceWindow.id).where(
                MaintenanceWindow.name == payload.name, MaintenanceWindow.id != window_id
            )
        )
    ).scalar_one_or_none()
    if name_clash is not None:
        return JSONResponse(
            status_code=409,
            content={"success": False, "error": f"A maintenance window named {payload.name!r} already exists"},
        )
    for field, value in payload.to_model_kwargs().items():
        setattr(window, field, value)
    await session.flush()
    # Re-materialize: an edit can start, move, or end the active occurrence.
    await sweep_maintenance_windows(session)
    add_audit(
        session,
        "maintenance_window",
        window.id,
        window.name,
        "updated",
        f"Maintenance window updated: {window.name}",
    )
    await session.commit()
    logger.info("[SilenceAPI] Maintenance window updated id=%s name=%s", window.id, window.name)
    active = window.enabled and active_occurrence(window, utcnow()) is not None
    return {
        "success": True,
        "data": maintenance_window_to_dict(window, active_now=active),
        "message": "Maintenance window updated",
    }


@silences_router.delete(
    "/maintenance-windows/{window_id}",
    response_model=None,
    dependencies=[Depends(verify_admin_write)],
)
async def delete_maintenance_window_endpoint(
    window_id: int, session: AsyncSession = Depends(get_db_session)
) -> JSONDict | JSONResponse:
    from models import MaintenanceWindow
    from services.silences.maintenance_windows import sweep_maintenance_windows

    window = await session.get(MaintenanceWindow, window_id)
    if window is None:
        return JSONResponse(status_code=404, content={"success": False, "error": "Maintenance window does not exist"})
    name = window.name
    await session.delete(window)
    await session.flush()
    # Lift any live silence this window materialized.
    await sweep_maintenance_windows(session)
    add_audit(
        session,
        "maintenance_window",
        window_id,
        name,
        "deleted",
        f"Maintenance window deleted: {name}",
    )
    await session.commit()
    logger.info("[SilenceAPI] Maintenance window deleted id=%s name=%s", window_id, name)
    return {"success": True, "message": "Maintenance window deleted"}


@silences_router.post(
    "/silences/backtest",
    response_model=SilenceBacktestResponse,
    dependencies=[Depends(verify_api_key)],
)
async def backtest_silence_endpoint(
    payload: SilenceBacktestRequest,
    session: AsyncSession = Depends(get_db_session),
) -> JSONDict:
    """Dry-run a proposed silence rule against historical database events."""
    logger.info(
        "[SilenceAPI] Proposed silence backtest lookback_days=%d source=%s project=%s",
        payload.lookback_days,
        payload.match_source,
        payload.match_project,
    )
    from services.operations.feature_adoption import record_feature_use

    await record_feature_use("action:silence_backtest_run")
    result = await backtest_silence_rule(
        session=session,
        match_source=payload.match_source,
        match_importance=payload.match_importance,
        match_event_type=payload.match_event_type,
        match_project=payload.match_project,
        match_region=payload.match_region,
        match_environment=payload.match_environment,
        match_payload=payload.match_payload,
        lookback_days=payload.lookback_days,
    )
    return {"success": True, "data": result}

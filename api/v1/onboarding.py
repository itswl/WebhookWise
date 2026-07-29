"""Inbound-source onboarding and scoped webhook ingress."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from api import internal_error_response, ok_response
from api.v1.webhook import JSONDict, receive_webhook
from core.app_context import get_config_manager
from core.auth import verify_admin_write
from core.logger import get_logger
from core.webhook_security import check_rate_limit_dep
from db.session import get_db_session
from models.source_connection import SourceConnection
from schemas.onboarding import (
    SourceConnectionActionRequest,
    SourceConnectionCreateRequest,
    SourceConnectionUpdateRequest,
)
from schemas.webhook import WebhookReceiveResponse
from services.webhooks.source_onboarding import (
    SourceCredentialRevokedError,
    connection_setup,
    create_source_connection,
    get_source_connection,
    list_source_connections,
    record_auth_failure,
    record_source_event,
    revoke_source_connection,
    rotate_source_token,
    source_by_public_id,
    source_connection_dict,
    source_templates,
    source_token_matches,
    update_source_connection,
)

logger = get_logger("api.v1.onboarding")

onboarding_router = APIRouter()
source_ingress_router = APIRouter()

_ONBOARDING_ERRORS = (IntegrityError, OSError, RuntimeError, SQLAlchemyError, TimeoutError, TypeError, ValueError)
_PUBLIC_ID_PATTERN = r"^src_[A-Za-z0-9_-]{8,32}$"


def _webhook_url(request: Request, public_id: str) -> str:
    return f"{str(request.base_url).rstrip('/')}/v1/source-webhooks/{public_id}"


def _token_candidates(request: Request) -> list[str]:
    candidates: list[str] = []

    def add(value: str | None) -> None:
        token = str(value or "").strip()
        if token and token not in candidates:
            candidates.append(token)

    authorization = request.headers.get("authorization", "").strip()
    if authorization:
        parts = authorization.split(None, 1)
        add(parts[1] if len(parts) == 2 and parts[0].lower() == "bearer" else authorization)
    add(request.headers.get("x-source-token"))
    add(request.headers.get("token"))
    return candidates


async def verify_source_connection_dep(
    request: Request,
    public_id: str = Path(..., pattern=_PUBLIC_ID_PATTERN),
    session: AsyncSession = Depends(get_db_session),
) -> SourceConnection:
    """Authenticate a scoped inbound source without granting management access."""
    connection = await source_by_public_id(session, public_id)
    if connection is not None and source_token_matches(connection, _token_candidates(request)):
        return connection
    connection_id = int(connection.id) if connection is not None else None
    try:
        await record_auth_failure(session, connection)
    except _ONBOARDING_ERRORS as error:
        try:
            await session.rollback()
        except _ONBOARDING_ERRORS:
            logger.error(
                "Failed to roll back inbound source authentication telemetry connection_id=%s",
                connection_id,
                exc_info=True,
            )
        logger.error(
            "Failed to record inbound source authentication telemetry connection_id=%s: %s",
            connection_id,
            error,
            exc_info=True,
        )
    logger.warning(
        "[SourceOnboarding] Scoped webhook authentication failed public_id=%s",
        public_id,
    )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or revoked source credential",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def _connection_or_404(session: AsyncSession, connection_id: int) -> SourceConnection:
    connection = await get_source_connection(session, connection_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="Source connection not found")
    return connection


@onboarding_router.get("/onboarding/source-types")
async def list_source_types_endpoint() -> JSONResponse:
    return ok_response(data=source_templates())


@onboarding_router.get("/onboarding/sources")
async def list_sources_endpoint(
    request: Request,
    limit: int = Query(100, ge=1, le=200),
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    try:
        rows = await list_source_connections(session, limit=limit + 1)
        has_more = len(rows) > limit
        data: list[dict[str, object]] = []
        for connection in rows[:limit]:
            item = source_connection_dict(connection)
            item["webhook_url"] = _webhook_url(request, connection.public_id)
            data.append(item)
        return ok_response(
            data=data,
            pagination={"has_more": has_more, "next_cursor": None, "page_size": limit},
        )
    except _ONBOARDING_ERRORS as error:
        logger.error("Failed to list inbound sources: %s", error, exc_info=True)
        return internal_error_response()


@onboarding_router.post(
    "/onboarding/sources",
    dependencies=[Depends(verify_admin_write)],
)
async def create_source_endpoint(
    payload: SourceConnectionCreateRequest,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    try:
        connection, token = await create_source_connection(session, payload)
        url = _webhook_url(request, connection.public_id)
        return ok_response(
            http_status=201,
            message="Inbound source connection created; copy the token now",
            data={
                "connection": source_connection_dict(connection),
                "setup": connection_setup(connection, url, plaintext_token=token),
            },
        )
    except _ONBOARDING_ERRORS as error:
        await session.rollback()
        logger.error("Failed to create inbound source: %s", error, exc_info=True)
        return internal_error_response()


@onboarding_router.get("/onboarding/sources/{connection_id}")
async def get_source_endpoint(
    connection_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    try:
        connection = await _connection_or_404(session, connection_id)
        url = _webhook_url(request, connection.public_id)
        return ok_response(
            data={
                "connection": source_connection_dict(connection),
                "setup": connection_setup(connection, url),
            }
        )
    except HTTPException:
        raise
    except _ONBOARDING_ERRORS as error:
        logger.error("Failed to read inbound source id=%s: %s", connection_id, error, exc_info=True)
        return internal_error_response()


@onboarding_router.patch(
    "/onboarding/sources/{connection_id}",
    dependencies=[Depends(verify_admin_write)],
)
async def update_source_endpoint(
    connection_id: int,
    payload: SourceConnectionUpdateRequest,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    try:
        connection = await _connection_or_404(session, connection_id)
        connection = await update_source_connection(session, connection, payload)
        return ok_response(
            message="Inbound source connection updated",
            data={
                "connection": source_connection_dict(connection),
                "setup": connection_setup(connection, _webhook_url(request, connection.public_id)),
            },
        )
    except HTTPException:
        raise
    except SourceCredentialRevokedError as error:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(error)) from error
    except _ONBOARDING_ERRORS as error:
        await session.rollback()
        logger.error("Failed to update inbound source id=%s: %s", connection_id, error, exc_info=True)
        return internal_error_response()


@onboarding_router.get("/onboarding/sources/{connection_id}/status")
async def source_status_endpoint(
    connection_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    try:
        connection = await _connection_or_404(session, connection_id)
        data = source_connection_dict(connection)
        data["webhook_url"] = _webhook_url(request, connection.public_id)
        return ok_response(data=data)
    except HTTPException:
        raise
    except _ONBOARDING_ERRORS as error:
        logger.error("Failed to read inbound source status id=%s: %s", connection_id, error, exc_info=True)
        return internal_error_response()


@onboarding_router.post(
    "/onboarding/sources/{connection_id}/rotate",
    dependencies=[Depends(verify_admin_write)],
)
async def rotate_source_endpoint(
    connection_id: int,
    payload: SourceConnectionActionRequest,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    try:
        connection = await _connection_or_404(session, connection_id)
        token = await rotate_source_token(session, connection, actor=payload.actor)
        return ok_response(
            message="Inbound source credential rotated; copy the token now",
            data={
                "connection": source_connection_dict(connection),
                "setup": connection_setup(
                    connection,
                    _webhook_url(request, connection.public_id),
                    plaintext_token=token,
                ),
            },
        )
    except HTTPException:
        raise
    except _ONBOARDING_ERRORS as error:
        await session.rollback()
        logger.error("Failed to rotate inbound source id=%s: %s", connection_id, error, exc_info=True)
        return internal_error_response()


@onboarding_router.post(
    "/onboarding/sources/{connection_id}/revoke",
    dependencies=[Depends(verify_admin_write)],
)
async def revoke_source_endpoint(
    connection_id: int,
    payload: SourceConnectionActionRequest,
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    try:
        connection = await _connection_or_404(session, connection_id)
        connection = await revoke_source_connection(session, connection, actor=payload.actor)
        return ok_response(
            message="Inbound source credential revoked",
            data=source_connection_dict(connection),
        )
    except HTTPException:
        raise
    except _ONBOARDING_ERRORS as error:
        await session.rollback()
        logger.error("Failed to revoke inbound source id=%s: %s", connection_id, error, exc_info=True)
        return internal_error_response()


@source_ingress_router.post(
    "/source-webhooks/{public_id}",
    dependencies=[Depends(check_rate_limit_dep)],
    response_model=WebhookReceiveResponse,
    status_code=200,
)
async def receive_managed_source_webhook(
    request: Request,
    connection: SourceConnection = Depends(verify_source_connection_dep),
    session: AsyncSession = Depends(get_db_session),
) -> JSONDict | JSONResponse:
    """Receive a webhook through a revocable source-scoped credential."""
    # Content-Length pre-check, mirroring the main ingress path: reject
    # oversized requests from the header before buffering the body.
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared = int(content_length)
        except ValueError:
            declared = None
        max_body_bytes = get_config_manager().security.MAX_WEBHOOK_BODY_BYTES
        if declared is not None and max_body_bytes and declared > max_body_bytes:
            return JSONResponse(
                status_code=413,
                content={
                    "success": False,
                    "error": f"Request body too large: {declared} bytes (max {max_body_bytes})",
                },
            )
    raw_body = await request.body()
    request.state.raw_body = raw_body
    request.state.source_connection_id = int(connection.id)
    request.state.webhook_source_scope = connection.public_id
    result = await receive_webhook(request, source=connection.source_type)
    if (
        isinstance(result, dict)
        and result.get("success")
        and result.get("request_id")
        # Structured outcome, not display-copy sniffing: only genuinely queued
        # events advance the connection's onboarding state.
        and result.get("outcome") == "queued"
    ):
        connection_id = int(connection.id)
        request_id = str(result["request_id"])
        try:
            await record_source_event(
                session,
                connection,
                request_id=request_id,
                raw_body=raw_body,
            )
        except _ONBOARDING_ERRORS as error:
            # The alert is already queued at this point. Returning an error for
            # optional onboarding telemetry would make upstreams retry and can
            # manufacture duplicate alerts, so keep the ingress result intact.
            try:
                await session.rollback()
            except _ONBOARDING_ERRORS as rollback_error:
                logger.error(
                    "Failed to roll back inbound source telemetry connection_id=%s: %s",
                    connection_id,
                    rollback_error,
                    exc_info=True,
                )
            logger.error(
                "Failed to record inbound source event after enqueue connection_id=%s request_id=%s: %s",
                connection_id,
                request_id,
                error,
                exc_info=True,
            )
    return result


__all__ = [
    "onboarding_router",
    "source_ingress_router",
    "verify_source_connection_dep",
]

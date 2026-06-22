"""Webhook payload test sandbox: dry-run a payload with zero side effects."""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from api import internal_error_response
from api.v1.webhook import JSONDict
from core.logger import get_logger
from db.session import get_db_session
from schemas.sandbox import SandboxTestRequest, SandboxTestResponse
from services.webhooks.sandbox import test_webhook_payload

logger = get_logger("api.v1.sandbox")

sandbox_router = APIRouter()

_SANDBOX_RUNTIME_ERRORS = (OSError, RuntimeError, SQLAlchemyError, TimeoutError, ValueError, TypeError)


@sandbox_router.post("/sandbox/test", response_model=SandboxTestResponse)
async def test_webhook_endpoint(
    request: SandboxTestRequest, session: AsyncSession = Depends(get_db_session)
) -> JSONDict | JSONResponse:
    """Dry-run a pasted webhook payload: report what WW would extract and decide.

    No enqueue, no AI call, no persistence — the only reads are the live forward
    rules and silences, so the verdict reflects the real configured routing.
    """
    try:
        logger.info("[Sandbox] Dry-run request source=%s", request.source or "(none)")
        data = await test_webhook_payload(session, source=request.source, payload=request.payload)
        return {"success": True, "data": data}
    except _SANDBOX_RUNTIME_ERRORS as e:
        logger.error("[Sandbox] Dry-run failed source=%s error=%s", request.source or "(none)", e, exc_info=True)
        return internal_error_response()

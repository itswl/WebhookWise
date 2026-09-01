"""Verified Feishu interactive-card callback endpoint."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from api import fail_response, internal_error_response
from core.auth import verify_feishu_card_callback
from core.logger import get_logger
from core.webhook_security import check_admin_rate_limit_dep
from db.session import get_db_session
from services.notifications.feishu_actions import (
    FeishuActionConflict,
    FeishuActionError,
    callback_payload_sha256,
    process_card_action,
)

logger = get_logger("api.v1.feishu_actions")

feishu_actions_router = APIRouter()


def _feishu_response(result: dict[str, object]) -> dict[str, object]:
    """Keep callback responses within Feishu's documented response fields."""
    return {key: value for key, value in result.items() if key in {"toast", "card"}}


@feishu_actions_router.post(
    "/integrations/feishu/card-actions",
    dependencies=[
        Depends(check_admin_rate_limit_dep),
        Depends(verify_feishu_card_callback),
    ],
)
async def feishu_card_action_callback(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """Verify and apply an idempotent operator action from a Feishu card."""
    payload: dict[str, Any] = request.state.feishu_card_payload
    challenge = payload.get("challenge")
    if challenge not in (None, ""):
        return JSONResponse(status_code=200, content={"challenge": str(challenge)})
    try:
        result = await process_card_action(
            session,
            payload,
            payload_sha256=callback_payload_sha256(request.state.feishu_card_body),
        )
        return JSONResponse(status_code=200, content=_feishu_response(result))
    except FeishuActionConflict as error:
        logger.warning("Rejected conflicting Feishu callback event: %s", error)
        return fail_response(str(error), 409)
    except FeishuActionError as error:
        logger.warning("Rejected invalid Feishu callback action: %s", error)
        return fail_response(str(error), 400)
    except (OSError, RuntimeError, SQLAlchemyError, TimeoutError, ValueError) as error:
        logger.error("Failed to process Feishu card action: %s", error, exc_info=True)
        return internal_error_response()

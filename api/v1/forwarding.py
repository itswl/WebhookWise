"""
Forwarding API Routes.
"""

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from api import DELIVERY_ERROR_MESSAGE, TARGET_URL_UNAVAILABLE_MESSAGE, internal_error_response
from core.auth import verify_admin_write, verify_api_key
from core.logger import get_logger, mask_url
from core.url_security import UnsafeTargetUrlError, validate_outbound_url
from db.session import get_db_session
from schemas.forwarding import (
    ForwardRuleCreateRequest,
    ForwardRuleDetailResponse,
    ForwardRuleListResponse,
    ForwardRuleUpdateRequest,
    forward_rule_to_dict,
)
from services.forwarding.rules import (
    create_forward_rule,
    delete_forward_rule,
    get_forward_rule,
    get_forward_rules,
    update_forward_rule,
)

logger = get_logger("api.v1.forwarding")

forwarding_router = APIRouter()

JSONDict = dict[str, object]
_FORWARDING_RUNTIME_ERRORS = (OSError, RuntimeError, SQLAlchemyError, TimeoutError, ValueError)


async def _validated_target_url(target_type: str, target_url: object) -> str:
    if target_type == "openclaw":
        return str(target_url or "").strip()
    if not isinstance(target_url, str) or not target_url.strip():
        raise UnsafeTargetUrlError("Target URL cannot be empty")
    return await validate_outbound_url(target_url)


# ── Forwarding Rules ─────────────────────────────────────────────────────────


@forwarding_router.get(
    "/forward-rules",
    response_model=ForwardRuleListResponse,
    dependencies=[Depends(verify_api_key)],
)
async def get_forward_rules_endpoint(session: AsyncSession = Depends(get_db_session)) -> JSONDict:
    rules = await get_forward_rules(session)
    return {"success": True, "data": [forward_rule_to_dict(rule, mask_target_url=True) for rule in rules]}


@forwarding_router.get(
    "/forward-rules/sensitive",
    response_model=ForwardRuleListResponse,
    dependencies=[Depends(verify_admin_write)],
)
async def get_sensitive_forward_rules_endpoint(session: AsyncSession = Depends(get_db_session)) -> JSONDict:
    rules = await get_forward_rules(session)
    return {"success": True, "data": [forward_rule_to_dict(rule) for rule in rules]}


@forwarding_router.post(
    "/forward-rules",
    response_model=ForwardRuleDetailResponse,
    dependencies=[Depends(verify_admin_write)],
)
async def create_forward_rule_endpoint(
    payload: ForwardRuleCreateRequest, session: AsyncSession = Depends(get_db_session)
) -> JSONDict | JSONResponse:
    data = payload.to_service_kwargs()
    name = data["name"]
    target_type = data["target_type"]

    try:
        target_url = await _validated_target_url(target_type, data["target_url"])
    except UnsafeTargetUrlError as e:
        logger.warning("[ForwardAPI] Create forward rule rejected name=%s target_type=%s error=%s", name, target_type, e)
        return JSONResponse(status_code=400, content={"success": False, "error": TARGET_URL_UNAVAILABLE_MESSAGE})

    rule = await create_forward_rule(
        session=session,
        name=name,
        target_type=target_type,
        enabled=data["enabled"],
        priority=data["priority"],
        match_event_type=data["match_event_type"],
        match_importance=data["match_importance"],
        match_duplicate=data["match_duplicate"],
        match_source=data["match_source"],
        match_project=data["match_project"],
        match_region=data["match_region"],
        match_environment=data["match_environment"],
        match_payload=data["match_payload"],
        target_url=target_url,
        target_name=data["target_name"],
        stop_on_match=data["stop_on_match"],
    )
    await session.commit()
    logger.info(
        "[ForwardAPI] Forward rule created rule_id=%s name=%s target_type=%s enabled=%s target=%s",
        rule.id,
        rule.name,
        rule.target_type,
        rule.enabled,
        mask_url(rule.target_url) if rule.target_url else "",
    )
    return {"success": True, "data": forward_rule_to_dict(rule), "message": "Rule created successfully"}


@forwarding_router.put(
    "/forward-rules/{rule_id}",
    response_model=ForwardRuleDetailResponse,
    dependencies=[Depends(verify_admin_write)],
)
async def update_forward_rule_endpoint(
    rule_id: int, payload: ForwardRuleUpdateRequest, session: AsyncSession = Depends(get_db_session)
) -> JSONDict | JSONResponse:
    data = payload.to_update_payload()
    existing = await get_forward_rule(session=session, rule_id=rule_id)
    if not existing:
        return JSONResponse(status_code=404, content={"success": False, "error": "Rule does not exist"})
    target_type = data.get("target_type", existing.target_type)
    if "target_url" in data or "target_type" in data:
        try:
            data["target_url"] = await _validated_target_url(target_type, data.get("target_url", existing.target_url))
        except UnsafeTargetUrlError as e:
            logger.warning(
                "[ForwardAPI] Update forward rule rejected rule_id=%s target_type=%s error=%s", rule_id, target_type, e
            )
            return JSONResponse(status_code=400, content={"success": False, "error": TARGET_URL_UNAVAILABLE_MESSAGE})
    rule = await update_forward_rule(session=session, rule_id=rule_id, payload=data)
    if rule is None:
        return JSONResponse(status_code=404, content={"success": False, "error": "Rule does not exist"})
    await session.commit()
    logger.info(
        "[ForwardAPI] Forward rule updated rule_id=%s name=%s target_type=%s enabled=%s target=%s",
        rule.id,
        rule.name,
        rule.target_type,
        rule.enabled,
        mask_url(rule.target_url) if rule.target_url else "",
    )
    return {"success": True, "data": forward_rule_to_dict(rule), "message": "Rule updated successfully"}


@forwarding_router.delete(
    "/forward-rules/{rule_id}",
    response_model=None,
    dependencies=[Depends(verify_admin_write)],
)
async def delete_forward_rule_endpoint(
    rule_id: int, session: AsyncSession = Depends(get_db_session)
) -> JSONDict | JSONResponse:
    if not await delete_forward_rule(session=session, rule_id=rule_id):
        return JSONResponse(status_code=404, content={"success": False, "error": "Rule does not exist"})
    await session.commit()
    logger.info("[ForwardAPI] Forward rule deleted rule_id=%s", rule_id)
    return {"success": True, "message": "Rule deleted"}


@forwarding_router.post(
    "/forward-rules/{rule_id}/test",
    response_model=None,
    dependencies=[Depends(verify_admin_write)],
)
async def test_forward_rule_endpoint(
    rule_id: int, session: AsyncSession = Depends(get_db_session)
) -> JSONDict | JSONResponse:
    rule = await get_forward_rule(session, rule_id)
    if not rule:
        return JSONResponse(status_code=404, content={"success": False, "error": "Rule does not exist"})

    target_url = str(rule.target_url or "").strip()
    if not target_url:
        return JSONResponse(status_code=400, content={"success": False, "error": "Rule has no target URL configured"})

    logger.info(
        "[ForwardAPI] Test forward rule rule_id=%s name=%s target_type=%s target=%s",
        rule.id,
        rule.name,
        rule.target_type,
        mask_url(target_url),
    )

    # Delivery (channel decision + payload build) lives in services/forwarding;
    # the API layer must not perform external delivery directly.
    from services.forwarding.remote import send_forward_rule_test

    try:
        result = await send_forward_rule_test(
            rule_name=rule.name,
            target_url=target_url,
            target_type=rule.target_type,
        )
    except _FORWARDING_RUNTIME_ERRORS as e:
        logger.warning("[ForwardAPI] Test forward request failed rule_id=%s error=%s", rule_id, e)
        return JSONResponse(status_code=502, content={"success": False, "error": DELIVERY_ERROR_MESSAGE})

    if result.get("status") == "success":
        return {"success": True, "message": "Test message delivered", "detail": result}
    logger.warning(
        "[ForwardAPI] Test forward not delivered rule_id=%s status=%s message=%s reason=%s",
        rule_id,
        result.get("status"),
        result.get("message"),
        result.get("reason"),
    )
    return JSONResponse(
        status_code=502,
        content={"success": False, "error": DELIVERY_ERROR_MESSAGE},
    )


# ── Outbox Queries ─────────────────────────────────────────────────────────


@forwarding_router.get(
    "/outbox",
    response_model=None,
    dependencies=[Depends(verify_admin_write)],
)
async def list_outbox_endpoint(
    page: int = Query(1, ge=1, le=100),
    page_size: int = Query(20, ge=1, le=200),
    cursor: int | None = Query(None),
    status: str = Query(""),
    event_type: str = Query(""),
) -> JSONDict | JSONResponse:
    """Query forwarding queue records."""
    from services.forwarding.outbox import list_outbox_records

    try:
        data = await list_outbox_records(
            page=page, page_size=page_size, cursor=cursor, status=status, event_type=event_type
        )
        return {"success": True, "data": data}
    except _FORWARDING_RUNTIME_ERRORS as e:
        logger.error("Failed to query outbox list: %s", e, exc_info=True)
        return internal_error_response()

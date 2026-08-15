"""Inbound rules: what to do with an alert on the way in.

The counterpart to /v1/forward-rules. Forwarding decides where an alert goes;
these decide what is spent on it before anyone looks — today, whether it reaches
the model and whether it funds an investigation.

Writes go through the same validation as the service layer and publish a cache
invalidation, so an edit is live everywhere within seconds without a restart.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from api import internal_error_response
from core.auth import verify_admin_write, verify_api_key
from core.logger import get_logger
from db.session import get_db_session
from models import InboundRule
from services.webhooks import inbound_rules as store

logger = get_logger("api.v1.inbound_rules")

inbound_rules_router = APIRouter()


def _fail(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse(status_code=status, content={"success": False, "error": message})


@inbound_rules_router.get("/inbound-rules", dependencies=[Depends(verify_api_key)])
async def list_inbound_rules(
    limit: int = Query(200, ge=1, le=500),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    rows = (
        (await session.execute(select(InboundRule).order_by(InboundRule.priority.desc(), InboundRule.id).limit(limit)))
        .scalars()
        .all()
    )
    return {
        "success": True,
        "data": [store.to_dict(rule) for rule in rows],
        "actions": sorted(store.INBOUND_ACTIONS),
    }


@inbound_rules_router.post("/inbound-rules", response_model=None, dependencies=[Depends(verify_admin_write)])
async def create_rule(
    payload: dict[str, Any],
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse | dict[str, Any]:
    problem = store.validate(payload)
    if problem:
        return _fail(problem)
    try:
        rule = await store.create_inbound_rule(session, payload)
        await session.commit()
    except SQLAlchemyError:
        logger.error("[InboundRules] create failed", exc_info=True)
        return internal_error_response()
    await store.publish_inbound_rules_invalidation()
    return {"success": True, "data": store.to_dict(rule)}


@inbound_rules_router.put("/inbound-rules/{rule_id}", response_model=None, dependencies=[Depends(verify_admin_write)])
async def update_rule(
    rule_id: int,
    payload: dict[str, Any],
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse | dict[str, Any]:
    existing = await session.get(InboundRule, rule_id)
    if existing is None:
        return _fail("inbound rule not found", 404)
    # Validate the rule as it WILL be, not as it was sent: a partial update must
    # not be able to leave a rule that can never match.
    merged = {**store.to_dict(existing), **payload}
    problem = store.validate(merged)
    if problem:
        return _fail(problem)
    try:
        rule = await store.update_inbound_rule(session, rule_id, payload)
        await session.commit()
    except SQLAlchemyError:
        logger.error("[InboundRules] update failed rule_id=%s", rule_id, exc_info=True)
        return internal_error_response()
    if rule is None:
        return _fail("inbound rule not found", 404)
    await store.publish_inbound_rules_invalidation()
    return {"success": True, "data": store.to_dict(rule)}


@inbound_rules_router.delete(
    "/inbound-rules/{rule_id}", response_model=None, dependencies=[Depends(verify_admin_write)]
)
async def delete_rule(
    rule_id: int,
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse | dict[str, Any]:
    try:
        removed = await store.delete_inbound_rule(session, rule_id)
        await session.commit()
    except SQLAlchemyError:
        logger.error("[InboundRules] delete failed rule_id=%s", rule_id, exc_info=True)
        return internal_error_response()
    if not removed:
        return _fail("inbound rule not found", 404)
    await store.publish_inbound_rules_invalidation()
    return {"success": True}

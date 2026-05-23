"""
Forwarding API Routes.
"""

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth import verify_admin_write
from core.logger import get_logger, mask_url
from core.url_security import UnsafeTargetUrlError, validate_outbound_url
from db.session import get_db_session
from schemas import (
    ForwardRuleDetailResponse,
    ForwardRuleListResponse,
    forward_rule_to_dict,
)
from services.forwarding.remote import forward_to_remote
from services.forwarding.rules import (
    create_forward_rule,
    delete_forward_rule,
    get_forward_rule,
    get_forward_rules,
    update_forward_rule,
)
from services.webhooks.types import AnalysisResult, WebhookData

logger = get_logger("api.forwarding")

forwarding_router = APIRouter()

JSONDict = dict[str, Any]


async def _validated_target_url(target_type: str, target_url: object) -> str:
    if target_type == "openclaw":
        return str(target_url or "").strip()
    if not isinstance(target_url, str) or not target_url.strip():
        raise UnsafeTargetUrlError("目标 URL 不能为空")
    return await validate_outbound_url(target_url)


# ── Forwarding Rules ─────────────────────────────────────────────────────────


@forwarding_router.get("/api/forward-rules", response_model=ForwardRuleListResponse)
async def get_forward_rules_endpoint(session: AsyncSession = Depends(get_db_session)) -> JSONDict:
    rules = await get_forward_rules(session)
    return {"success": True, "data": [forward_rule_to_dict(rule) for rule in rules]}


@forwarding_router.post(
    "/api/forward-rules",
    response_model=ForwardRuleDetailResponse,
    dependencies=[Depends(verify_admin_write)],
)
async def create_forward_rule_endpoint(
    payload: dict[str, Any] | None = None, session: AsyncSession = Depends(get_db_session)
) -> JSONDict | JSONResponse:
    payload = payload or {}
    name = payload.get("name", "").strip() if isinstance(payload.get("name"), str) else ""
    target_type = payload.get("target_type", "").strip() if isinstance(payload.get("target_type"), str) else ""

    if not name:
        return JSONResponse(status_code=400, content={"success": False, "error": "规则名称不能为空"})
    if target_type not in ("feishu", "openclaw", "webhook"):
        return JSONResponse(status_code=400, content={"success": False, "error": "目标类型无效"})
    try:
        target_url = await _validated_target_url(target_type, payload.get("target_url", ""))
    except UnsafeTargetUrlError as e:
        logger.warning("[ForwardAPI] 创建转发规则被拒绝 name=%s target_type=%s error=%s", name, target_type, e)
        return JSONResponse(status_code=400, content={"success": False, "error": str(e)})

    rule = await create_forward_rule(
        session=session,
        name=name,
        target_type=target_type,
        enabled=payload.get("enabled", True),
        priority=payload.get("priority", 0),
        match_importance=payload.get("match_importance", ""),
        match_duplicate=payload.get("match_duplicate", "all"),
        match_source=payload.get("match_source", ""),
        match_payload=payload.get("match_payload", ""),
        target_url=target_url,
        target_name=payload.get("target_name", ""),
        stop_on_match=payload.get("stop_on_match", False),
    )
    await session.commit()
    logger.info(
        "[ForwardAPI] 转发规则已创建 rule_id=%s name=%s target_type=%s enabled=%s target=%s",
        rule.id,
        rule.name,
        rule.target_type,
        rule.enabled,
        mask_url(rule.target_url) if rule.target_url else "",
    )
    return {"success": True, "data": forward_rule_to_dict(rule), "message": "规则创建成功"}


@forwarding_router.put(
    "/api/forward-rules/{rule_id}",
    response_model=ForwardRuleDetailResponse,
    dependencies=[Depends(verify_admin_write)],
)
async def update_forward_rule_endpoint(
    rule_id: int, payload: dict[str, Any] | None = None, session: AsyncSession = Depends(get_db_session)
) -> JSONDict | JSONResponse:
    payload = payload or {}
    existing = await get_forward_rule(session=session, rule_id=rule_id)
    if not existing:
        return JSONResponse(status_code=404, content={"success": False, "error": "规则不存在"})
    target_type = payload.get("target_type", existing.target_type)
    if not isinstance(target_type, str) or target_type not in ("feishu", "openclaw", "webhook"):
        return JSONResponse(status_code=400, content={"success": False, "error": "目标类型无效"})
    if "target_url" in payload or "target_type" in payload:
        try:
            payload = dict(payload)
            payload["target_url"] = await _validated_target_url(
                target_type, payload.get("target_url", existing.target_url)
            )
        except UnsafeTargetUrlError as e:
            logger.warning(
                "[ForwardAPI] 更新转发规则被拒绝 rule_id=%s target_type=%s error=%s", rule_id, target_type, e
            )
            return JSONResponse(status_code=400, content={"success": False, "error": str(e)})
    rule = await update_forward_rule(session=session, rule_id=rule_id, payload=payload)
    if rule is None:
        return JSONResponse(status_code=404, content={"success": False, "error": "规则不存在"})
    await session.commit()
    logger.info(
        "[ForwardAPI] 转发规则已更新 rule_id=%s name=%s target_type=%s enabled=%s target=%s",
        rule.id,
        rule.name,
        rule.target_type,
        rule.enabled,
        mask_url(rule.target_url) if rule.target_url else "",
    )
    return {"success": True, "data": forward_rule_to_dict(rule), "message": "规则更新成功"}


@forwarding_router.delete(
    "/api/forward-rules/{rule_id}",
    response_model=None,
    dependencies=[Depends(verify_admin_write)],
)
async def delete_forward_rule_endpoint(
    rule_id: int, session: AsyncSession = Depends(get_db_session)
) -> JSONDict | JSONResponse:
    if not await delete_forward_rule(session=session, rule_id=rule_id):
        return JSONResponse(status_code=404, content={"success": False, "error": "规则不存在"})
    await session.commit()
    logger.info("[ForwardAPI] 转发规则已删除 rule_id=%s", rule_id)
    return {"success": True, "message": "规则已删除"}


@forwarding_router.post(
    "/api/forward-rules/{rule_id}/test",
    response_model=None,
    dependencies=[Depends(verify_admin_write)],
)
async def test_forward_rule_endpoint(
    rule_id: int, session: AsyncSession = Depends(get_db_session)
) -> JSONDict | JSONResponse:
    rule = await get_forward_rule(session, rule_id)
    if not rule:
        return JSONResponse(status_code=404, content={"success": False, "error": "规则不存在"})

    test_webhook: WebhookData = {"source": "test", "parsed_data": {"test": True, "rule_name": rule.name}}
    test_analysis: AnalysisResult = {"summary": f"测试规则: {rule.name}", "importance": "low", "event_type": "test"}

    if rule.target_type == "openclaw":
        from services.forwarding.openclaw import forward_to_openclaw

        logger.info("[ForwardAPI] 测试 OpenClaw 转发规则 rule_id=%s name=%s", rule.id, rule.name)
        result = await forward_to_openclaw(test_webhook, test_analysis)
    else:
        try:
            target_url = await _validated_target_url(rule.target_type, rule.target_url)
        except UnsafeTargetUrlError as e:
            return JSONResponse(status_code=400, content={"success": False, "error": str(e)})
        logger.info(
            "[ForwardAPI] 测试转发规则 rule_id=%s name=%s target_type=%s target=%s",
            rule.id,
            rule.name,
            rule.target_type,
            mask_url(target_url),
        )
        result = await forward_to_remote(test_webhook, test_analysis, target_url=target_url)

    if result.get("status") == "success" or result.get("_pending"):
        return {"success": True, "message": "测试消息已发送", "detail": result}
    elif result.get("status") == "skipped":
        return JSONResponse(status_code=400, content={"success": False, "error": "目标 URL 未配置"})
    else:
        return JSONResponse(status_code=502, content={"success": False, "error": result.get("message", "发送失败")})

"""转发规则 CRUD 路由。"""

from datetime import datetime

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from services.forward import (
    create_forward_rule,
    delete_forward_rule,
    get_forward_rule,
    get_forward_rules,
    update_forward_rule,
)
from db.session import get_db_session
from schemas.rules import ForwardRuleDetailResponse, ForwardRuleListResponse

forward_rules_router = APIRouter()


# ── 路由 ─────────────────────────────────────────────────────────────────────


@forward_rules_router.get("/api/forward-rules", response_model=ForwardRuleListResponse)
async def get_forward_rules_endpoint(session: AsyncSession = Depends(get_db_session)):
    rules = await get_forward_rules(session)
    return {"success": True, "data": rules}


@forward_rules_router.post("/api/forward-rules", response_model=ForwardRuleDetailResponse)
async def create_forward_rule_endpoint(payload: dict | None = None, session: AsyncSession = Depends(get_db_session)):
    payload = payload or {}
    name = payload.get("name", "").strip() if isinstance(payload.get("name"), str) else ""
    target_type = payload.get("target_type", "").strip() if isinstance(payload.get("target_type"), str) else ""

    if not name:
        return JSONResponse(status_code=400, content={"success": False, "error": "规则名称不能为空"})
    if target_type not in ("feishu", "openclaw", "webhook"):
        return JSONResponse(
            status_code=400, content={"success": False, "error": "目标类型必须为 feishu/openclaw/webhook"}
        )
    if target_type != "openclaw":
        target_url = payload.get("target_url", "").strip() if isinstance(payload.get("target_url"), str) else ""
        if not target_url:
            return JSONResponse(status_code=400, content={"success": False, "error": "目标地址不能为空"})

    rule = await create_forward_rule(
        session=session,
        name=name,
        target_type=target_type,
        enabled=payload.get("enabled", True),
        priority=payload.get("priority", 0),
        match_importance=payload.get("match_importance", ""),
        match_duplicate=payload.get("match_duplicate", "all"),
        match_source=payload.get("match_source", ""),
        target_url=payload.get("target_url", ""),
        target_name=payload.get("target_name", ""),
        stop_on_match=payload.get("stop_on_match", False),
    )
    return {"success": True, "data": rule, "message": "规则创建成功"}


@forward_rules_router.put("/api/forward-rules/{rule_id}", response_model=ForwardRuleDetailResponse)
async def update_forward_rule_endpoint(
    rule_id: int, payload: dict | None = None, session: AsyncSession = Depends(get_db_session)
):
    payload = payload or {}
    rule = await update_forward_rule(session=session, rule_id=rule_id, payload=payload)
    if not rule:
        return JSONResponse(status_code=404, content={"success": False, "error": "规则不存在"})
    return {"success": True, "data": rule, "message": "规则更新成功"}


@forward_rules_router.delete("/api/forward-rules/{rule_id}")
async def delete_forward_rule_endpoint(rule_id: int, session: AsyncSession = Depends(get_db_session)):
    success = await delete_forward_rule(session=session, rule_id=rule_id)
    if not success:
        return JSONResponse(status_code=404, content={"success": False, "error": "规则不存在"})
    return {"success": True, "message": "规则已删除"}


@forward_rules_router.post("/api/forward-rules/{rule_id}/test")
async def test_forward_rule(rule_id: int, session: AsyncSession = Depends(get_db_session)):
    rule = await get_forward_rule(session=session, rule_id=rule_id)
    if not rule:
        return JSONResponse(status_code=404, content={"success": False, "error": "规则不存在"})

    test_data = {
        "source": "test",
        "parsed_data": {"message": "这是一条转发规则测试消息", "rule_name": rule.name},
        "timestamp": datetime.now().isoformat(),
    }
    test_analysis = {"importance": "medium", "summary": f"转发规则测试 - {rule.name}", "event_type": "test"}

    if rule.target_type == "openclaw":
        from services.forward import forward_to_openclaw
        result = await forward_to_openclaw(test_data, test_analysis)
    else:
        from services.forward import forward_to_remote
        result = await forward_to_remote(test_data, test_analysis, target_url=rule.target_url)

    return {"success": True, "data": result, "message": "测试完成"}

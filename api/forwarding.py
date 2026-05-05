"""
Forwarding API Routes.
Consolidated from forward_rules and forward_retry.
"""


from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from api import _fail, _ok
from db.session import get_db_session
from schemas import (
    ForwardRuleDetailResponse,
    ForwardRuleListResponse,
)
from services.forward import (
    create_forward_rule,
    delete_failed_forward,
    delete_forward_rule,
    forward_to_remote,
    get_failed_forward_stats,
    get_failed_forwards,
    get_forward_rule,
    get_forward_rules,
    manual_retry_reset,
    update_forward_rule,
)

forwarding_router = APIRouter()


# ── Forwarding Rules ─────────────────────────────────────────────────────────


@forwarding_router.get("/api/forward-rules", response_model=ForwardRuleListResponse)
async def get_forward_rules_endpoint(session: AsyncSession = Depends(get_db_session)):
    rules = await get_forward_rules(session)
    return {"success": True, "data": [r.to_dict() for r in rules]}


@forwarding_router.post("/api/forward-rules", response_model=ForwardRuleDetailResponse)
async def create_forward_rule_endpoint(payload: dict | None = None, session: AsyncSession = Depends(get_db_session)):
    payload = payload or {}
    name = payload.get("name", "").strip() if isinstance(payload.get("name"), str) else ""
    target_type = payload.get("target_type", "").strip() if isinstance(payload.get("target_type"), str) else ""

    if not name:
        return JSONResponse(status_code=400, content={"success": False, "error": "规则名称不能为空"})
    if target_type not in ("feishu", "openclaw", "webhook"):
        return JSONResponse(status_code=400, content={"success": False, "error": "目标类型无效"})

    rule = await create_forward_rule(
        session=session, name=name, target_type=target_type,
        enabled=payload.get("enabled", True), priority=payload.get("priority", 0),
        match_importance=payload.get("match_importance", ""),
        match_duplicate=payload.get("match_duplicate", "all"),
        match_source=payload.get("match_source", ""),
        target_url=payload.get("target_url", ""),
        target_name=payload.get("target_name", ""),
        stop_on_match=payload.get("stop_on_match", False),
    )
    return {"success": True, "data": rule, "message": "规则创建成功"}


@forwarding_router.put("/api/forward-rules/{rule_id}", response_model=ForwardRuleDetailResponse)
async def update_forward_rule_endpoint(
    rule_id: int, payload: dict | None = None, session: AsyncSession = Depends(get_db_session)
):
    payload = payload or {}
    rule = await update_forward_rule(session=session, rule_id=rule_id, payload=payload)
    if not rule:
        return JSONResponse(status_code=404, content={"success": False, "error": "规则不存在"})
    return {"success": True, "data": rule, "message": "规则更新成功"}


@forwarding_router.delete("/api/forward-rules/{rule_id}")
async def delete_forward_rule_endpoint(rule_id: int, session: AsyncSession = Depends(get_db_session)):
    if not await delete_forward_rule(session=session, rule_id=rule_id):
        return JSONResponse(status_code=404, content={"success": False, "error": "规则不存在"})
    return {"success": True, "message": "规则已删除"}


@forwarding_router.post("/api/forward-rules/{rule_id}/test")
async def test_forward_rule_endpoint(rule_id: int, session: AsyncSession = Depends(get_db_session)):
    rule = await get_forward_rule(session, rule_id)
    if not rule:
        return JSONResponse(status_code=404, content={"success": False, "error": "规则不存在"})

    test_webhook = {"source": "test", "parsed_data": {"test": True, "rule_name": rule.name}}
    test_analysis = {"summary": f"测试规则: {rule.name}", "importance": "low", "event_type": "test"}

    target_url = rule.target_url if rule.target_type != "openclaw" else None
    result = await forward_to_remote(test_webhook, test_analysis, target_url=target_url)

    if result.get("status") == "success":
        return {"success": True, "message": "测试消息已发送", "detail": result}
    elif result.get("status") == "skipped":
        return JSONResponse(status_code=400, content={"success": False, "error": "目标 URL 未配置"})
    else:
        return JSONResponse(status_code=502, content={"success": False, "error": result.get("message", "发送失败")})


# ── Forwarding Retry ─────────────────────────────────────────────────────────


@forwarding_router.get("/api/failed-forwards")
async def list_failed_forwards(
    status: str = Query(None),
    target_type: str = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db_session)
):
    records, total = await get_failed_forwards(status, target_type, limit, offset, session)
    return _ok(data=records, total=total, limit=limit, offset=offset)


@forwarding_router.get("/api/failed-forwards/stats")
async def get_retry_stats(session: AsyncSession = Depends(get_db_session)):
    return _ok(data=await get_failed_forward_stats(session))


@forwarding_router.post("/api/failed-forwards/{failed_forward_id}/retry")
async def retry_forward(failed_forward_id: int, session: AsyncSession = Depends(get_db_session)):
    if await manual_retry_reset(failed_forward_id, session):
        return _ok(message="已重置为待重试")
    return _fail("记录不存在或状态不是 exhausted", 400)


@forwarding_router.delete("/api/failed-forwards/{failed_forward_id}")
async def delete_record(failed_forward_id: int, session: AsyncSession = Depends(get_db_session)):
    if await delete_failed_forward(failed_forward_id, session):
        return _ok(message="记录已删除")
    return _fail("记录不存在", 404)

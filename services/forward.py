"""
Forwarding Service.
Handles event forwarding to remote targets, retries, and rule management.
"""

import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.circuit_breaker import forward_cb, openclaw_cb
from core.config import Config
from core.http_client import get_http_client
from core.logger import logger
from db.session import count_with_timeout, session_scope
from models import FailedForward, ForwardRule

# 类型别名
WebhookData = dict[str, Any]
AnalysisResult = dict[str, Any]
ForwardResult = dict[str, Any]


async def get_forward_rules(session: AsyncSession):
    stmt = select(ForwardRule).order_by(ForwardRule.priority.desc())
    result = await session.execute(stmt)
    return result.scalars().all()


async def create_forward_rule(
    session: AsyncSession,
    name: str,
    target_type: str,
    enabled: bool = True,
    priority: int = 0,
    match_importance: str = "",
    match_duplicate: str = "all",
    match_source: str = "",
    target_url: str = "",
    target_name: str = "",
    stop_on_match: bool = False,
):
    rule = ForwardRule(
        name=name,
        enabled=enabled,
        priority=priority,
        match_importance=match_importance,
        match_duplicate=match_duplicate,
        match_source=match_source,
        target_type=target_type,
        target_url=target_url,
        target_name=target_name,
        stop_on_match=stop_on_match,
    )
    session.add(rule)
    await session.flush()
    return rule


async def get_forward_rule(session: AsyncSession, rule_id: int):
    stmt = select(ForwardRule).filter_by(id=rule_id)
    result = await session.execute(stmt)
    return result.scalars().first()


async def update_forward_rule(session: AsyncSession, rule_id: int, payload: dict):
    rule = await get_forward_rule(session, rule_id)
    if not rule:
        return None

    fields = [
        "name", "enabled", "priority", "match_importance", "match_duplicate",
        "match_source", "target_type", "target_url", "target_name", "stop_on_match",
    ]
    for field in fields:
        if field in payload:
            setattr(rule, field, payload[field])

    rule.updated_at = datetime.now()
    await session.flush()
    return rule


async def delete_forward_rule(session: AsyncSession, rule_id: int):
    rule = await get_forward_rule(session, rule_id)
    if not rule:
        return False
    await session.delete(rule)
    return True


async def record_failed_forward(
    webhook_event_id: int,
    forward_rule_id: int | None,
    target_url: str,
    target_type: str,
    failure_reason: str,
    error_message: str | None = None,
    forward_data: dict | None = None,
    forward_headers: dict | None = None,
    max_retries: int | None = None,
    session: AsyncSession | None = None,
) -> FailedForward | None:
    """写入转发失败记录，计算首次重试时间"""
    if max_retries is None:
        max_retries = Config.retry.FORWARD_RETRY_MAX_RETRIES

    now = datetime.now()
    next_retry_at = now + timedelta(seconds=Config.retry.FORWARD_RETRY_INITIAL_DELAY)

    record = FailedForward(
        webhook_event_id=webhook_event_id,
        forward_rule_id=forward_rule_id,
        target_url=target_url,
        target_type=target_type,
        status="pending",
        failure_reason=failure_reason,
        error_message=error_message,
        retry_count=0,
        max_retries=max_retries,
        next_retry_at=next_retry_at,
        forward_data=forward_data,
        forward_headers=forward_headers,
        created_at=now,
        updated_at=now,
    )

    try:
        if session is not None:
            session.add(record)
            await session.flush()
            logger.info(f"转发失败记录已写入: ID={record.id}, target={target_url}")
            return record

        async with session_scope() as scoped_session:
            scoped_session.add(record)
            await scoped_session.flush()
            logger.info(f"转发失败记录已写入: ID={record.id}, target={target_url}")
            return record
    except Exception as e:
        logger.error(f"写入转发失败记录失败: {e!s}")
        return None


async def get_failed_forwards(
    status: str | None = None,
    target_type: str | None = None,
    limit: int = 20,
    offset: int = 0,
    session: AsyncSession | None = None,
) -> tuple[list[dict], int]:
    """按状态/类型分页查询转发失败记录"""

    async def _query(sess: AsyncSession) -> tuple[list[dict], int]:
        conditions = []
        if status:
            conditions.append(FailedForward.status == status)
        if target_type:
            conditions.append(FailedForward.target_type == target_type)

        count_stmt = select(func.count()).select_from(FailedForward)
        for cond in conditions:
            count_stmt = count_stmt.filter(cond)
        total = await count_with_timeout(sess, count_stmt) or 0

        query = select(FailedForward)
        for cond in conditions:
            query = query.filter(cond)
        query = query.order_by(FailedForward.next_retry_at.asc()).offset(offset).limit(limit)
        result = await sess.execute(query)
        records = result.scalars().all()
        return [r.to_dict() for r in records], total

    if session:
        return await _query(session)
    async with session_scope() as scoped_session:
        return await _query(scoped_session)


async def get_failed_forward_stats(session: AsyncSession | None = None) -> dict[str, int]:
    async def _query(sess: AsyncSession) -> dict[str, int]:
        stmt = select(FailedForward.status, func.count()).group_by(FailedForward.status)
        result = await sess.execute(stmt)
        rows = result.all()
        stats = {"pending": 0, "retrying": 0, "success": 0, "exhausted": 0, "total": 0}
        for status_val, count in rows:
            if status_val in stats:
                stats[status_val] = count
            stats["total"] += count
        return stats
    if session:
        return await _query(session)
    async with session_scope() as scoped_session:
        return await _query(scoped_session)


async def manual_retry_reset(failed_forward_id: int, session: AsyncSession | None = None) -> bool:
    async def _reset(sess: AsyncSession) -> bool:
        record = await sess.get(FailedForward, failed_forward_id)
        if not record or record.status != "exhausted":
            return False
        now = datetime.now()
        record.status, record.retry_count, record.updated_at = "pending", 0, now
        record.next_retry_at = now + timedelta(seconds=Config.retry.FORWARD_RETRY_INITIAL_DELAY)
        await sess.flush()
        return True
    if session:
        return await _reset(session)
    async with session_scope() as scoped_session:
        return await _reset(scoped_session)


async def delete_failed_forward(failed_forward_id: int, session: AsyncSession | None = None) -> bool:
    async def _delete(sess: AsyncSession) -> bool:
        record = await sess.get(FailedForward, failed_forward_id)
        if not record:
            return False
        await sess.delete(record)
        await sess.flush()
        return True
    if session:
        return await _delete(session)
    async with session_scope() as scoped_session:
        return await _delete(scoped_session)


async def cleanup_old_success_records(days: int = 7, session: AsyncSession | None = None) -> int:
    cutoff = datetime.now() - timedelta(days=days)
    async def _cleanup(sess: AsyncSession) -> int:
        stmt = sa_delete(FailedForward).where(FailedForward.status == "success").where(FailedForward.updated_at < cutoff)
        result = await sess.execute(stmt)
        count = result.rowcount
        await sess.flush()
        return count
    if session:
        return await _cleanup(session)
    async with session_scope() as scoped_session:
        return await _cleanup(scoped_session)


# ── 转发执行 ─────────────────────────────────────────────────────────────


async def forward_to_remote(
    webhook_data: WebhookData,
    analysis_result: AnalysisResult,
    target_url: str | None = None,
    is_periodic_reminder: bool = False,
) -> ForwardResult:
    """转发分析结果到远程 Webhook URL (支持飞书卡片自动格式化)。"""
    url = target_url or Config.ai.FORWARD_URL
    if not url:
        logger.debug("[Forward] 无转发 URL，跳过")
        return {"status": "skipped", "reason": "no_forward_url"}

    # 飞书/Lark 自动格式化
    is_feishu = "feishu.cn" in url or "larksuite.com" in url
    if is_feishu:
        from adapters.plugins.feishu_card import build_feishu_card
        payload = build_feishu_card(webhook_data, analysis_result, is_periodic_reminder=is_periodic_reminder)
    else:
        payload = {"webhook": webhook_data, "analysis": analysis_result, "is_periodic_reminder": is_periodic_reminder}

    async def _do_post():
        client = get_http_client()
        logger.debug("[Forward] POST %s is_feishu=%s periodic=%s", url, is_feishu, is_periodic_reminder)
        resp = await client.post(url, json=payload, timeout=Config.ai.FORWARD_TIMEOUT)
        resp.raise_for_status()
        return resp

    try:
        response = await forward_cb.call_async(_do_post)
        if response is None:
            logger.warning("[Forward] 熔断器已开启，转发被拦截 url=%s", url)
            return {"status": "circuit_broken", "message": "熔断器已开启"}

        return {
            "status": "success", "status_code": response.status_code,
            "response": response.json() if response.content else {}
        }
    except Exception as e:
        logger.error("[Forward] 转发失败 url=%s error=%s", url, e)
        return {"status": "failed", "message": str(e)}


async def forward_to_openclaw(webhook_data: WebhookData, analysis_result: dict) -> dict:
    """推送任务到 OpenClaw 进行深度分析。"""
    if not Config.openclaw.OPENCLAW_ENABLED:
        logger.debug("[Forward] OpenClaw 未启用，跳过深度分析")
        return {"status": "disabled"}

    async def _do_request():
        from adapters.plugins.openclaw_engine import OpenClawAnalysisEngine
        engine = OpenClawAnalysisEngine()
        return await engine.analyze(
            webhook_data.get("parsed_data", {}),
            source=webhook_data.get("source", "unknown"),
            headers=webhook_data.get("headers", {})
        )

    try:
        res = await openclaw_cb.call_async(_do_request)
        if res is None:
            return {"status": "circuit_broken"}
        return res
    except Exception as e:
        logger.error(f"OpenClaw 转发异常: {e}")
        return {"status": "error", "message": str(e)}


async def analyze_with_openclaw(webhook_data: dict, user_question: str = "", thinking_level: str = "high") -> dict:
    """通过 OpenClaw Agent 进行深度分析（非阻塞触发，立即返回）"""
    from core.trace import get_trace_id

    if not Config.openclaw.OPENCLAW_ENABLED:
        logger.warning("OpenClaw 未启用")
        return {"_degraded": True, "_degraded_reason": "OpenClaw 未启用"}

    alert_data = webhook_data.get("parsed_data", {})
    source = webhook_data.get("source", "unknown")

    prompt_path = Path(Config.server.DATA_DIR).parent / "prompts" / "deep_analysis.txt"
    try:
        with open(prompt_path, encoding="utf-8") as f:
            template = f.read()
    except FileNotFoundError:
        template = "请对以下告警进行深度根因分析：\n\n{source}\n{alert_data}\n"
        logger.warning(f"未能找到深度分析模板文件: {prompt_path}")

    message = f"{template}\n\n## 当前告警数据\n告警来源: {source}\n```json\n{json.dumps(alert_data, ensure_ascii=False, separators=(',', ':'))}\n```"
    if user_question:
        message += f"\n\n## 用户补充问题\n{user_question}"

    session_key = f"hook:deep-analysis:{source}:{uuid.uuid4()}"
    payload = {
        "message": message,
        "name": "deep-analysis",
        "sessionKey": session_key,
        "wakeMode": "now",
        "deliver": False,
        "thinking": thinking_level,
        "timeoutSeconds": Config.openclaw.OPENCLAW_TIMEOUT_SECONDS,
    }

    platform = getattr(Config.ai, "DEEP_ANALYSIS_PLATFORM", "openclaw").lower()
    hooks_token = Config.openclaw.OPENCLAW_HOOKS_TOKEN or Config.openclaw.OPENCLAW_GATEWAY_TOKEN

    if platform == "hermes":
        import hashlib
        import hmac as hmac_mod
        target_url = f"{Config.openclaw.OPENCLAW_GATEWAY_URL}/webhooks/agent"
        payload_bytes = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        signature = hmac_mod.new(hooks_token.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
        headers = {"Content-Type": "application/json", "X-Webhook-Signature": signature}
        kwargs: dict = {"content": payload_bytes}
    else:
        target_url = f"{Config.openclaw.OPENCLAW_GATEWAY_URL}/hooks/agent"
        headers = {"Authorization": f"Bearer {hooks_token}", "Content-Type": "application/json"}
        kwargs = {"json": payload}

    trace_id = get_trace_id()
    if trace_id:
        headers["X-Trace-Id"] = trace_id

    logger.info(f"[{platform.upper()}] 正在发起分析请求: target={target_url}")

    max_retries = 3
    last_error = None
    response = None

    for attempt in range(max_retries):
        try:
            client = get_http_client()
            response = await openclaw_cb.call_async(
                client.post, target_url, headers=headers, timeout=httpx.Timeout(60.0, connect=10.0), **kwargs
            )
            if response is None:
                last_error = f"{platform.capitalize()} 请求被熔断器拦截"
                if attempt < max_retries - 1:
                    import asyncio
                    await asyncio.sleep(2)
                continue
            response.raise_for_status()
            break
        except Exception as e:
            last_error = str(e)
            logger.warning(f"{platform.capitalize()} 请求异常 (尝试 {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                import asyncio
                await asyncio.sleep(2)
    else:
        logger.error(f"{platform.capitalize()} 请求失败，已重试 {max_retries} 次: {last_error}")
        if Config.ai.ENABLE_AI_DEGRADATION:
            return {"_degraded": True, "_degraded_reason": f"{platform.capitalize()} 请求失败: {last_error}"}
        raise Exception(f"{platform.capitalize()} 请求失败: {last_error}")

    try:
        result = response.json()
        if platform == "hermes":
            run_id = result.get("delivery_id") or result.get("runId")
            session_key = run_id if run_id else session_key
        else:
            run_id = result.get("runId")
        logger.info(f"[{platform.upper()}] 成功触发深度分析: ID={run_id}")
        return {"_pending": True, "_openclaw_run_id": run_id, "_openclaw_session_key": session_key}
    except Exception as e:
        logger.error(f"OpenClaw 响应解析失败: {e}")
        if Config.ai.ENABLE_AI_DEGRADATION:
            return {"_degraded": True, "_degraded_reason": f"响应解析失败: {e!s}"}
        raise

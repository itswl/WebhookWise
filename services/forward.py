"""转发逻辑模块

负责将分析结果转发到远程服务器（飞书、通用 HTTP）、
以及 OpenClaw Agent 深度分析触发。
"""

import json
import logging
from pathlib import Path
from typing import Any

import httpx

from core.config import Config
from core.config import policies
from core.http_client import get_http_client
from core.trace import get_trace_id
from core.circuit_breaker import forward_cb, openclaw_cb

logger = logging.getLogger("webhook_service.forward")

# 类型别名
WebhookData = dict[str, Any]
AnalysisResult = dict[str, Any]
ForwardResult = dict[str, Any]


from datetime import datetime, timedelta

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from db.session import count_with_timeout, session_scope
from models import FailedForward, ForwardRule, WebhookEvent


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

    for field in [
        "name", "enabled", "priority", "match_importance", "match_duplicate",
        "match_source", "target_type", "target_url", "target_name", "stop_on_match",
    ]:
        if field in payload:
            setattr(rule, field, payload[field])

    rule.updated_at = datetime.now()
    await session.flush()
    return rule


async def delete_forward_rule(session: AsyncSession, rule_id: int):
    rule = await get_forward_rule(session, rule_id)
    if not rule:
        return False
    session.delete(rule)
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
        if status: conditions.append(FailedForward.status == status)
        if target_type: conditions.append(FailedForward.target_type == target_type)

        count_stmt = select(func.count()).select_from(FailedForward)
        for cond in conditions: count_stmt = count_stmt.filter(cond)
        total = await count_with_timeout(sess, count_stmt)

        query = select(FailedForward)
        for cond in conditions: query = query.filter(cond)
        query = query.order_by(FailedForward.next_retry_at.asc()).offset(offset).limit(limit)
        result = await sess.execute(query)
        records = result.scalars().all()
        return [r.to_dict() for r in records], total

    if session: return await _query(session)
    async with session_scope() as scoped_session: return await _query(scoped_session)


async def get_failed_forward_stats(session: AsyncSession | None = None) -> dict[str, int]:
    async def _query(sess: AsyncSession) -> dict[str, int]:
        stmt = select(FailedForward.status, func.count()).group_by(FailedForward.status)
        result = await sess.execute(stmt)
        rows = result.all()
        stats = {"pending": 0, "retrying": 0, "success": 0, "exhausted": 0, "total": 0}
        for status_val, count in rows:
            if status_val in stats: stats[status_val] = count
            stats["total"] += count
        return stats
    if session: return await _query(session)
    async with session_scope() as scoped_session: return await _query(scoped_session)


async def manual_retry_reset(failed_forward_id: int, session: AsyncSession | None = None) -> bool:
    async def _reset(sess: AsyncSession) -> bool:
        record = await sess.get(FailedForward, failed_forward_id)
        if not record or record.status != "exhausted": return False
        now = datetime.now()
        record.status, record.retry_count, record.updated_at = "pending", 0, now
        record.next_retry_at = now + timedelta(seconds=Config.retry.FORWARD_RETRY_INITIAL_DELAY)
        await sess.flush()
        return True
    if session: return await _reset(session)
    async with session_scope() as scoped_session: return await _reset(scoped_session)


async def delete_failed_forward(failed_forward_id: int, session: AsyncSession | None = None) -> bool:
    async def _delete(sess: AsyncSession) -> bool:
        record = await sess.get(FailedForward, failed_forward_id)
        if not record: return False
        await sess.delete(record); await sess.flush()
        return True
    if session: return await _delete(session)
    async with session_scope() as scoped_session: return await _delete(scoped_session)


async def cleanup_old_success_records(days: int = 7, session: AsyncSession | None = None) -> int:
    cutoff = datetime.now() - timedelta(days=days)
    async def _cleanup(sess: AsyncSession) -> int:
        stmt = sa_delete(FailedForward).where(FailedForward.status == "success").where(FailedForward.updated_at < cutoff)
        result = await sess.execute(stmt); count = result.rowcount
        await sess.flush()
        return count
    if session: return await _cleanup(session)
    async with session_scope() as scoped_session: return await _cleanup(scoped_session)


async def forward_to_remote(
    webhook_data: WebhookData,
    analysis_result: AnalysisResult,
    target_url: str | None = None,
    is_periodic_reminder: bool = False,
) -> ForwardResult:
    """将分析后的数据转发到远程服务器

    Args:
        webhook_data: Webhook 数据
        analysis_result: AI 分析结果
        target_url: 目标 URL
        is_periodic_reminder: 是否为周期性提醒
    """
    # 检查是否启用转发
    if not policies.ai.ENABLE_FORWARD:
        logger.info("转发功能已禁用")
        return {"status": "disabled", "message": "转发功能已禁用"}

    if target_url is None:
        target_url = policies.ai.FORWARD_URL

    try:
        # 检查是否是飞书 webhook
        is_feishu = "feishu.cn" in target_url or "lark" in target_url

        if is_feishu:
            # 构建飞书消息格式
            forward_data = build_feishu_message(
                webhook_data, analysis_result, is_periodic_reminder=is_periodic_reminder
            )
        else:
            # 构建普通转发数据
            forward_data = {
                "original_data": webhook_data.get("parsed_data", {}),
                "original_source": webhook_data.get("source", "unknown"),
                "original_timestamp": webhook_data.get("timestamp"),
                "ai_analysis": analysis_result,
                "processed_by": "webhook-analyzer",
                "client_ip": webhook_data.get("client_ip"),
            }

        # 发送到远程服务器
        headers = {"Content-Type": "application/json"}
        trace_id = get_trace_id()
        if trace_id:
            headers["X-Trace-Id"] = trace_id

        if not is_feishu:
            headers["X-Webhook-Source"] = f"analyzed-{webhook_data.get('source', 'unknown')}"
            headers["X-Analysis-Importance"] = analysis_result.get("importance", "unknown")

        logger.info(f"转发数据到 {target_url}")
        client = get_http_client()
        response = await forward_cb.call_async(
            client.post,
            target_url,
            json=forward_data,
            headers=headers,
            timeout=Config.server.FORWARD_REQUEST_TIMEOUT_SECONDS,
        )

        if response is None:
            return {"status": "failed", "message": "转发请求被熔断拦截"}

        if 200 <= response.status_code < 300:
            logger.info(f"成功转发到远程服务器: {target_url} (状态码: {response.status_code})")
            return {
                "status": "success",
                "response": response.json() if response.content else {},
                "status_code": response.status_code,
            }
        else:
            logger.warning(f"转发失败,状态码: {response.status_code}")
            return {"status": "failed", "status_code": response.status_code, "response": response.text}

    except httpx.TimeoutException:
        logger.error(f"转发超时: {target_url}")
        return {"status": "timeout", "message": "请求超时"}
    except httpx.ConnectError:
        logger.error(f"无法连接到远程服务器: {target_url}")
        return {"status": "connection_error", "message": "无法连接到远程服务器"}
    except Exception as e:
        logger.error(f"转发失败: {e!s}", exc_info=True)
        return {"status": "error", "message": str(e)}


def build_feishu_message(
    webhook_data: WebhookData, analysis_result: AnalysisResult, is_periodic_reminder: bool = False
) -> dict:
    """构建飞书机器人消息格式

    Args:
        webhook_data: Webhook 数据
        analysis_result: AI 分析结果
        is_periodic_reminder: 是否为周期性提醒
    """
    # 获取基本信息
    source = webhook_data.get("source", "unknown")
    timestamp = webhook_data.get("timestamp", "")
    importance = analysis_result.get("importance", "medium")
    summary = analysis_result.get("summary", "无摘要")
    event_type = analysis_result.get("event_type", "未知事件")
    duplicate_count = webhook_data.get("duplicate_count", 1)

    # 使用配置中的重要性配置
    imp_info = Config.ai.IMPORTANCE_CONFIG.get(importance, Config.ai.IMPORTANCE_CONFIG["medium"])

    # 标题：如果是周期性提醒，添加特殊标识
    if is_periodic_reminder:
        title = f"🔔 周期性提醒：告警持续中（已重复 {duplicate_count} 次）"
    else:
        title = "📡 Webhook 事件通知"

    # 构建卡片消息
    card_content = {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": title}, "template": imp_info["color"]},
        "elements": [
            {
                "tag": "div",
                "fields": [
                    {"is_short": True, "text": {"tag": "lark_md", "content": f"**来源**\n{source}"}},
                    {
                        "is_short": True,
                        "text": {"tag": "lark_md", "content": f"**重要性**\n{imp_info['emoji']} {imp_info['text']}"},
                    },
                    {"is_short": True, "text": {"tag": "lark_md", "content": f"**事件类型**\n{event_type}"}},
                    {
                        "is_short": True,
                        "text": {"tag": "lark_md", "content": f"**时间**\n{timestamp[:19] if timestamp else '-'}"},
                    },
                ],
            },
            {"tag": "div", "text": {"tag": "lark_md", "content": f"**📝 事件摘要**\n{summary}"}},
        ],
    }

    # 添加影响范围
    if analysis_result.get("impact_scope"):
        card_content["elements"].append(
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"**🎯 影响范围**\n{analysis_result.get('impact_scope')}"},
            }
        )

    # 添加建议操作
    if analysis_result.get("actions"):
        actions_text = "\n".join([f"{i + 1}. {action}" for i, action in enumerate(analysis_result.get("actions", []))])
        card_content["elements"].append(
            {"tag": "div", "text": {"tag": "lark_md", "content": f"**✅ 建议操作**\n{actions_text}"}}
        )

    return {"msg_type": "interactive", "card": card_content}


async def forward_to_openclaw(webhook_data: dict, analysis_result: dict) -> dict:
    """将告警推送到 OpenClaw 触发深度分析（非阻塞触发，立即返回）"""
    from core.config import Config

    if not Config.openclaw.OPENCLAW_ENABLED:
        return {"status": "disabled", "message": "OpenClaw 未启用"}

    alert_data = webhook_data.get("parsed_data", {})
    source = webhook_data.get("source", "unknown")
    importance = analysis_result.get("importance", "medium") if analysis_result else "medium"

    message = f"""收到新告警，请自主排查分析：

来源: {source}
重要性: {importance}

## 告警数据
```json
{json.dumps(alert_data, ensure_ascii=False, separators=(",", ":"))}
```

## AI 初步分析
{json.dumps(analysis_result, ensure_ascii=False, indent=2) if analysis_result else "无"}

## 指令
你可以自主使用 MCP 工具和 Skills 进行排查：
- 根据告警内容，自行决定需要查询哪些数据、执行哪些排查命令
- 如果涉及 Kubernetes，可以使用 kubectl 相关能力查看 Pod/Node/Service 状态
- 如果涉及监控指标，可以查询 Prometheus/Grafana 获取历史数据
- 分析完成后，提供根因分析和可执行的修复建议"""

    import uuid

    session_key = f"hook:alert:{source}:{uuid.uuid4()}"
    payload = {
        "message": message,
        "name": f"alert-{source}",
        "sessionKey": session_key,
        "wakeMode": "now",
        "deliver": False,
        "thinking": "high",
        "timeoutSeconds": Config.openclaw.OPENCLAW_TIMEOUT_SECONDS,
    }

    # 适配不同的调用平台 (OpenClaw 或 Hermes)
    platform = getattr(Config.ai, "DEEP_ANALYSIS_PLATFORM", "openclaw").lower()
    hooks_token = Config.openclaw.OPENCLAW_HOOKS_TOKEN or Config.openclaw.OPENCLAW_GATEWAY_TOKEN

    if platform == "hermes":
        import hashlib
        import hmac

        target_url = f"{Config.openclaw.OPENCLAW_GATEWAY_URL}/webhooks/agent"
        payload_bytes = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        signature = hmac.new(hooks_token.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
        headers = {"Content-Type": "application/json", "X-Webhook-Signature": signature}
        kwargs = {"content": payload_bytes}
    else:
        # Default OpenClaw
        target_url = f"{Config.openclaw.OPENCLAW_GATEWAY_URL}/hooks/agent"
        headers = {"Authorization": f"Bearer {hooks_token}", "Content-Type": "application/json"}
        kwargs = {"json": payload}

    trace_id = get_trace_id()
    if trace_id:
        headers["X-Trace-Id"] = trace_id

    try:
        import hashlib

        payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        payload_size = len(payload_json)
    except Exception:
        payload_hash = None
        payload_size = len(str(payload))
    logger.info(
        f"[{platform.upper()}] 正在发起分析请求: target={target_url}, size={payload_size}, sha256={payload_hash}"
    )
    # 超时配置：(连接超时, 读取超时)
    client = get_http_client()
    response = await openclaw_cb.call_async(
        client.post, target_url, headers=headers, timeout=httpx.Timeout(60.0, connect=10.0), **kwargs
    )

    if response is None:
        return {"status": "error", "message": f"{platform.capitalize()} 请求被熔断拦截"}

    try:
        # response.raise_for_status() was already called inside the loop, so it's guaranteed to be OK here.
        result = response.json()

        # 兼容两种协议的返回 ID
        if platform == "hermes":
            run_id = result.get("delivery_id") or result.get("runId")
            session_key = run_id if run_id else session_key
        else:
            run_id = result.get("runId")

        logger.info(f"[{platform.upper()}] 转发成功: run_id={run_id}")

        return {"status": "success", "run_id": run_id, "session_key": session_key, "_pending": True}
    except Exception as e:
        logger.error(f"OpenClaw 转发失败: {e}")
        return {"status": "error", "message": str(e)}


async def analyze_with_openclaw(webhook_data: dict, user_question: str = "", thinking_level: str = "high") -> dict:
    """通过 OpenClaw Agent 进行深度分析（非阻塞触发，立即返回）"""
    from core.config import Config

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
        template = """请对以下告警进行深度根因分析：

{source}
{alert_data}
"""
        logger.warning(f"未能找到深度分析模板文件: {prompt_path}")

    # 将告警数据注入到提示词中
    message = f"{template}\n\n## 当前告警数据\n告警来源: {source}\n```json\n{json.dumps(alert_data, ensure_ascii=False, separators=(',', ':'))}\n```"

    if user_question:
        message += f"\n\n## 用户补充问题\n{user_question}"

    import uuid

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

    # 适配不同的调用平台 (OpenClaw 或 Hermes)
    platform = getattr(Config.ai, "DEEP_ANALYSIS_PLATFORM", "openclaw").lower()
    hooks_token = Config.openclaw.OPENCLAW_HOOKS_TOKEN or Config.openclaw.OPENCLAW_GATEWAY_TOKEN

    if platform == "hermes":
        import hashlib
        import hmac

        target_url = f"{Config.openclaw.OPENCLAW_GATEWAY_URL}/webhooks/agent"
        payload_bytes = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        signature = hmac.new(hooks_token.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
        headers = {"Content-Type": "application/json", "X-Webhook-Signature": signature}
        kwargs = {"content": payload_bytes}
    else:
        target_url = f"{Config.openclaw.OPENCLAW_GATEWAY_URL}/hooks/agent"
        headers = {"Authorization": f"Bearer {hooks_token}", "Content-Type": "application/json"}
        kwargs = {"json": payload}

    trace_id = get_trace_id()
    if trace_id:
        headers["X-Trace-Id"] = trace_id

    logger.info(f"[{platform.upper()}] 正在发起分析请求: target={target_url}, len={len(str(payload))}")
    logger.debug(f"[{platform.upper()}] 完整载荷内容: {payload}")
    # 重试逻辑：最多 3 次
    max_retries = 3
    last_error = None

    for attempt in range(max_retries):
        try:
            client = get_http_client()
            response = await openclaw_cb.call_async(
                client.post, target_url, headers=headers, timeout=httpx.Timeout(60.0, connect=10.0), **kwargs
            )

            if response is None:
                last_error = f"{platform.capitalize()} 请求失败（熔断器拦截或服务不可用）"
                logger.warning(f"{platform.capitalize()} 请求失败 (尝试 {attempt + 1}/{max_retries})")
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
            continue
    else:
        logger.error(f"{platform.capitalize()} 请求失败，已重试 {max_retries} 次: {last_error}")
        try:
            from sqlalchemy import select

            from db.session import session_scope
            from models import WebhookEvent

            if Config.ai.DEEP_ANALYSIS_FEISHU_WEBHOOK:
                async with session_scope() as session:
                    from sqlalchemy import select

                    stmt = select(WebhookEvent).filter_by(id=webhook_data.get("id"))
                    result = await session.execute(stmt)
                    event = result.scalars().first()
                    source = event.source if event else "unknown"
                from services.ai_analyzer import _send_openclaw_failure_notification

                await _send_openclaw_failure_notification(webhook_data, source, last_error)
        except Exception as notify_err:
            logger.warning(f"发送 {platform.capitalize()} 失败通知失败: {notify_err}")

        if Config.ai.ENABLE_AI_DEGRADATION:
            logger.warning(f"{platform.capitalize()} 请求失败，降级到本地 AI 分析")
            return {"_degraded": True, "_degraded_reason": f"{platform.capitalize()} 请求失败: {last_error}"}
        else:
            logger.error(f"{platform.capitalize()} 请求失败，未启用降级策略")
            raise Exception(f"{platform.capitalize()} 请求失败: {last_error}")

    try:
        # response.raise_for_status() was already called inside the loop, so it's guaranteed to be OK here.
        result = response.json()

        if platform == "hermes":
            run_id = result.get("delivery_id") or result.get("runId")
            session_key = run_id if run_id else session_key
        else:
            run_id = result.get("runId")

        logger.info(f"[{platform.upper()}] 成功触发深度分析: ID={run_id}")

        return {"_pending": True, "_openclaw_run_id": run_id, "_openclaw_session_key": session_key}
    except httpx.RequestError as e:
        logger.error(f"OpenClaw 请求失败: {e}")
        # 根据配置决定是否降级
        if Config.ai.ENABLE_AI_DEGRADATION:
            logger.warning("OpenClaw 请求失败，降级到本地 AI 分析")
            return {"_degraded": True, "_degraded_reason": f"OpenClaw 不可用: {e!s}"}
        else:
            logger.error("OpenClaw 请求失败，未启用降级策略")
            raise

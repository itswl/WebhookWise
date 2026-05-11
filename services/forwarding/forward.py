"""
Forwarding Service.
Handles event forwarding to remote targets, retries, and rule management.
"""

import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar

import httpx
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.circuit_breaker import CircuitBreakerOpenException, forward_cb, openclaw_cb
from core.config import Config
from core.http_client import get_http_client
from core.logger import logger
from core.url_security import UnsafeTargetUrlError, validate_outbound_url
from core.utils import is_feishu_url
from db.session import count_with_timeout, session_scope
from models import FailedForward, ForwardRule
from services.webhooks.types import AnalysisResult, FailedForwardStatus, ForwardResult, WebhookData

_T = TypeVar("_T")
_JSON_UTF8_CONTENT_TYPE = "application/json; charset=utf-8"


async def _with_session(
    session: AsyncSession | None, fn: Callable[..., Awaitable[_T]], *args: Any, **kwargs: Any
) -> _T:
    """Run *fn* with either a provided session or a newly scoped one."""
    if session is not None:
        return await fn(session, *args, **kwargs)
    async with session_scope() as scoped:
        return await fn(scoped, *args, **kwargs)


async def _schedule_failed_forward_retry(record_id: int, delay_seconds: int) -> None:
    if not Config.retry.ENABLE_FORWARD_RETRY:
        return
    try:
        from services.operations.taskiq_retry_scheduler import schedule_forward_retry

        await schedule_forward_retry(record_id, delay_seconds)
    except Exception as e:
        logger.warning("[ForwardRetry] TaskIQ 重试调度失败 record_id=%s error=%s", record_id, e)


async def get_forward_rules(session: AsyncSession) -> list[ForwardRule]:
    stmt = select(ForwardRule).order_by(ForwardRule.priority.desc())
    result = await session.execute(stmt)
    return list(result.scalars().all())


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
) -> ForwardRule:
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


async def get_forward_rule(session: AsyncSession, rule_id: int) -> ForwardRule | None:
    stmt = select(ForwardRule).filter_by(id=rule_id)
    result = await session.execute(stmt)
    return result.scalars().first()


async def update_forward_rule(session: AsyncSession, rule_id: int, payload: dict[str, Any]) -> ForwardRule | None:
    rule = await get_forward_rule(session, rule_id)
    if not rule:
        return None

    fields = [
        "name",
        "enabled",
        "priority",
        "match_importance",
        "match_duplicate",
        "match_source",
        "target_type",
        "target_url",
        "target_name",
        "stop_on_match",
    ]
    for field in fields:
        if field in payload:
            setattr(rule, field, payload[field])

    rule.updated_at = datetime.now()
    await session.flush()
    return rule


async def delete_forward_rule(session: AsyncSession, rule_id: int) -> bool:
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
    forward_data: dict[str, Any] | None = None,
    forward_headers: dict[str, Any] | None = None,
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
        status=FailedForwardStatus.PENDING,
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

    async def _persist(sess: AsyncSession) -> FailedForward:
        sess.add(record)
        await sess.flush()
        await _schedule_failed_forward_retry(record.id, Config.retry.FORWARD_RETRY_INITIAL_DELAY)
        return record

    try:
        persisted = await _with_session(session, _persist)
        logger.info("转发失败记录已写入: ID=%s, target=%s", persisted.id, target_url)
        return persisted
    except Exception as e:
        logger.error("写入转发失败记录失败: %s", e)
        return None


async def get_failed_forwards(
    status: str | None = None,
    target_type: str | None = None,
    limit: int = 20,
    offset: int = 0,
    session: AsyncSession | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """按状态/类型分页查询转发失败记录"""

    async def _query(sess: AsyncSession) -> tuple[list[dict[str, Any]], int]:
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

    return await _with_session(session, _query)


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

    return await _with_session(session, _query)


async def manual_retry_reset(failed_forward_id: int, session: AsyncSession | None = None) -> bool:
    async def _reset(sess: AsyncSession) -> bool:
        record = await sess.get(FailedForward, failed_forward_id)
        if not record or record.status != FailedForwardStatus.EXHAUSTED:
            return False
        now = datetime.now()
        record.status, record.retry_count, record.updated_at = FailedForwardStatus.PENDING, 0, now
        record.next_retry_at = now + timedelta(seconds=Config.retry.FORWARD_RETRY_INITIAL_DELAY)
        await sess.flush()
        await _schedule_failed_forward_retry(record.id, Config.retry.FORWARD_RETRY_INITIAL_DELAY)
        return True

    return await _with_session(session, _reset)


async def delete_failed_forward(failed_forward_id: int, session: AsyncSession | None = None) -> bool:
    async def _delete(sess: AsyncSession) -> bool:
        record = await sess.get(FailedForward, failed_forward_id)
        if not record:
            return False
        await sess.delete(record)
        await sess.flush()
        return True

    return await _with_session(session, _delete)


async def cleanup_old_success_records(days: int = 7, session: AsyncSession | None = None) -> int:
    cutoff = datetime.now() - timedelta(days=days)

    async def _cleanup(sess: AsyncSession) -> int:
        stmt = (
            sa_delete(FailedForward)
            .where(FailedForward.status == FailedForwardStatus.SUCCESS)
            .where(FailedForward.updated_at < cutoff)
        )
        result = await sess.execute(stmt)
        count = int(result.rowcount or 0)
        await sess.flush()
        return count

    return await _with_session(session, _cleanup)


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
    try:
        url = await validate_outbound_url(url)
    except UnsafeTargetUrlError as e:
        logger.warning("[Forward] 目标 URL 安全校验失败 url=%s error=%s", url, e)
        return {"status": "invalid_target", "message": str(e)}

    # 飞书/Lark 自动格式化
    is_feishu = is_feishu_url(url)
    if is_feishu:
        from adapters.plugins.feishu_card import build_feishu_card

        payload = build_feishu_card(webhook_data, analysis_result, is_periodic_reminder=is_periodic_reminder)
    else:
        payload = {"webhook": webhook_data, "analysis": analysis_result, "is_periodic_reminder": is_periodic_reminder}

    async def _do_post() -> httpx.Response:
        client = get_http_client()
        logger.debug("[Forward] POST %s is_feishu=%s periodic=%s", url, is_feishu, is_periodic_reminder)
        resp = await client.post(url, json=payload, timeout=Config.ai.FORWARD_TIMEOUT)
        resp.raise_for_status()
        return resp

    try:
        response = await forward_cb.call_async(_do_post)
        resp_payload: dict[str, Any] = {}
        if response.content:
            try:
                raw_json = response.json()
                resp_payload = raw_json if isinstance(raw_json, dict) else {"_raw": raw_json}
            except ValueError:
                resp_payload = {"_raw": response.text[:1000]}
        return {
            "status": "success",
            "status_code": response.status_code,
            "response": resp_payload,
        }
    except CircuitBreakerOpenException:
        logger.warning("[Forward] 熔断器已开启，转发被拦截 url=%s", url)
        return {"status": "circuit_broken", "message": "熔断器已开启"}
    except Exception as e:
        logger.error("[Forward] 转发失败 url=%s error=%s", url, e)
        return {"status": "failed", "message": str(e)}


async def forward_to_openclaw(webhook_data: WebhookData, analysis_result: AnalysisResult) -> ForwardResult:
    """推送任务到 OpenClaw 进行深度分析。"""
    if not Config.openclaw.OPENCLAW_ENABLED:
        logger.debug("[Forward] OpenClaw 未启用，跳过深度分析")
        return {"status": "disabled"}

    async def _do_request() -> dict[str, Any]:
        from services.analysis.ai_analyzer import analyze_webhook_with_ai

        result = await analyze_with_openclaw(webhook_data)
        if result.get("_degraded"):
            logger.warning("[Forward] OpenClaw 降级，回退本地 AI: %s", result.get("_degraded_reason"))
            local_data = {
                "source": webhook_data.get("source", "unknown"),
                "headers": webhook_data.get("headers", {}),
                "parsed_data": webhook_data.get("parsed_data", {}),
            }
            return await analyze_webhook_with_ai(local_data)
        return result

    try:
        res = await openclaw_cb.call_async(_do_request)
        return res
    except CircuitBreakerOpenException:
        return {"status": "circuit_broken"}
    except Exception as e:
        logger.error("OpenClaw 转发异常: %s", e)
        return {"status": "error", "message": str(e)}


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _extract_openclaw_overview(source: str, alert_data: dict[str, Any]) -> dict[str, Any]:
    first_alert: dict[str, Any] = {}
    alerts = alert_data.get("alerts")
    if isinstance(alerts, list) and alerts and isinstance(alerts[0], dict):
        first_alert = alerts[0]

    labels = _dict_or_empty(first_alert.get("labels"))
    annotations = _dict_or_empty(first_alert.get("annotations"))
    overview: dict[str, Any] = {
        "source": source,
        "type": alert_data.get("Type"),
        "rule_name": alert_data.get("RuleName") or labels.get("alertname") or alert_data.get("alertingRuleName"),
        "level": alert_data.get("Level") or labels.get("severity") or labels.get("internal_label_alert_level"),
        "summary": alert_data.get("summary") or annotations.get("summary") or annotations.get("description"),
    }
    if labels:
        overview["labels"] = labels
    if annotations:
        overview["annotations"] = annotations
    if first_alert:
        overview["prometheus_alert"] = {
            "status": first_alert.get("status"),
            "startsAt": first_alert.get("startsAt"),
            "endsAt": first_alert.get("endsAt"),
            "generatorURL": first_alert.get("generatorURL"),
            "fingerprint": first_alert.get("fingerprint") or labels.get("internal_label_alert_id"),
        }
    return {k: v for k, v in overview.items() if v not in (None, "", {}, [])}


def _build_openclaw_prompt_payload(source: str, alert_data: dict[str, Any]) -> dict[str, Any]:
    overview = _extract_openclaw_overview(source, alert_data)
    return {"overview": overview, "payload": alert_data}


async def analyze_with_openclaw(
    webhook_data: WebhookData, user_question: str = "", thinking_level: str = "high"
) -> dict[str, Any]:
    """通过 OpenClaw Agent 进行深度分析（非阻塞触发，立即返回）"""
    from core.trace import get_trace_id

    if not Config.openclaw.OPENCLAW_ENABLED:
        logger.warning("OpenClaw 未启用")
        return {"_degraded": True, "_degraded_reason": "OpenClaw 未启用"}

    alert_data = webhook_data.get("parsed_data", {})
    source = webhook_data.get("source", "unknown")
    if not isinstance(alert_data, dict):
        alert_data = {"raw": alert_data}
    from services.webhooks.payload_sanitizer import sanitize_for_ai_async

    alert_data = await sanitize_for_ai_async(alert_data, strip_configured_keys=False, truncate=False)
    prompt_payload = _build_openclaw_prompt_payload(str(source), alert_data)

    prompt_path = Path(Config.server.DATA_DIR).parent / "prompts" / "deep_analysis.txt"
    try:
        with open(prompt_path, encoding="utf-8") as f:
            template = f.read()
    except FileNotFoundError:
        template = "请对以下告警进行深度根因分析：\n\n{source}\n{alert_data}\n"
        logger.warning("未能找到深度分析模板文件: %s", prompt_path)

    overview_json = json.dumps(prompt_payload.get("overview", {}), ensure_ascii=False, separators=(",", ":"))
    payload_json = json.dumps(prompt_payload, ensure_ascii=False, separators=(",", ":"))
    message = (
        f"{template}\n\n"
        "## 当前告警关键字段（优先使用）\n"
        f"告警来源: {source}\n"
        "```json\n"
        f"{overview_json}\n"
        "```\n\n"
        "## 当前告警数据\n"
        "下面的 payload 仅做敏感字段脱敏，不做大小裁剪；若网关或模型显示层发生截断，请基于上方关键字段继续排查，不要要求用户重新粘贴。\n"
        "```json\n"
        f"{payload_json}\n"
        "```"
    )
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
    payload_bytes = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    connect_timeout = max(1.0, float(Config.openclaw.OPENCLAW_CONNECT_TIMEOUT))

    if platform == "hermes":
        import hashlib
        import hmac as hmac_mod

        target_url = f"{Config.openclaw.OPENCLAW_GATEWAY_URL}/webhooks/agent"
        signature = hmac_mod.new(hooks_token.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
        headers = {"Content-Type": _JSON_UTF8_CONTENT_TYPE, "X-Webhook-Signature": signature}
    else:
        target_url = f"{Config.openclaw.OPENCLAW_GATEWAY_URL}/hooks/agent"
        headers = {"Authorization": f"Bearer {hooks_token}", "Content-Type": _JSON_UTF8_CONTENT_TYPE}
    kwargs: dict[str, Any] = {"content": payload_bytes}

    trace_id = get_trace_id()
    if trace_id:
        headers["X-Trace-Id"] = trace_id

    if not hooks_token:
        logger.warning("[%s] OpenClaw token 为空，将按当前配置继续发起请求", platform.upper())
    logger.info(
        "[%s] 正在发起分析请求: target=%s session_key=%s payload_bytes=%s trace_id=%s",
        platform.upper(),
        target_url,
        session_key,
        len(payload_bytes),
        trace_id or "-",
    )

    max_retries = 3
    last_error = None
    response: httpx.Response | None = None

    for attempt in range(max_retries):
        try:
            client = get_http_client()
            response = await openclaw_cb.call_async(
                client.post,
                target_url,
                headers=headers,
                timeout=httpx.Timeout(60.0, connect=connect_timeout),
                **kwargs,
            )
            response.raise_for_status()
            break
        except CircuitBreakerOpenException as e:
            last_error = str(e)
            logger.warning("%s 请求被熔断器拦截: %s", platform.capitalize(), e)
            if Config.ai.ENABLE_AI_DEGRADATION:
                return {"_degraded": True, "_degraded_reason": f"{platform.capitalize()} 请求失败: {last_error}"}
            raise
        except Exception as e:
            last_error = str(e)
            logger.warning("%s 请求异常 (尝试 %d/%d): %s", platform.capitalize(), attempt + 1, max_retries, e)
            if attempt < max_retries - 1:
                import asyncio

                await asyncio.sleep(2)
    else:
        logger.error("%s 请求失败，已重试 %d 次: %s", platform.capitalize(), max_retries, last_error)
        if Config.ai.ENABLE_AI_DEGRADATION:
            return {"_degraded": True, "_degraded_reason": f"{platform.capitalize()} 请求失败: {last_error}"}
        raise Exception(f"{platform.capitalize()} 请求失败: {last_error}")

    if response is None:
        raise RuntimeError(f"{platform.capitalize()} 请求失败: empty response")

    try:
        raw = response.json()
        if not isinstance(raw, dict):
            raise ValueError("OpenClaw response is not a JSON object")
        result: dict[str, Any] = raw
        if platform == "hermes":
            run_id = result.get("delivery_id") or result.get("runId")
            session_key = run_id if run_id else session_key
        else:
            run_id = result.get("runId")
        logger.info("[%s] 成功触发深度分析: ID=%s", platform.upper(), run_id)
        return {"_pending": True, "_openclaw_run_id": run_id, "_openclaw_session_key": session_key}
    except Exception as e:
        logger.error("OpenClaw 响应解析失败: %s", e)
        if Config.ai.ENABLE_AI_DEGRADATION:
            return {"_degraded": True, "_degraded_reason": f"响应解析失败: {e!s}"}
        raise

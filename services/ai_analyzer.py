"""AI 分析编排层

作为主入口，协调缓存、AI 调用、规则降级等子模块，
提供 analyze_webhook_with_ai 和 analyze_with_rules 两个核心函数。
"""

import logging
import time
from typing import Any

import httpx
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from core.config import Config
from core.config import policies
from core.logger import logger
from core.metrics import AI_ANALYSIS_DURATION_SECONDS, sanitize_source
from services.ai_cache import get_cached_analysis, log_ai_usage, save_to_cache
from services.ai_client import _send_degradation_alert, analyze_with_openai_tracked

# 类型别名
WebhookData = dict[str, Any]
AnalysisResult = dict[str, Any]
ForwardResult = dict[str, Any]


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential_jitter(initial=2, max=30, jitter=2),
    retry=retry_if_exception(
        lambda e: isinstance(e, (httpx.RequestError, httpx.TimeoutException, ConnectionError, TimeoutError))
    ),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
async def _call_ai_with_retry(parsed_data: dict[str, Any], source: str) -> tuple[dict[str, Any], int, int]:
    """带指数退避重试的 AI 调用"""
    start_time = time.time()
    analysis, tokens_in, tokens_out = await analyze_with_openai_tracked(parsed_data, source)
    duration = time.time() - start_time
    AI_ANALYSIS_DURATION_SECONDS.labels(source=sanitize_source(source), engine="openai").observe(duration)
    logger.info(f"AI 分析完成: {source}")
    return analysis, tokens_in, tokens_out


import math
from datetime import datetime, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from db.session import count_with_timeout
from models import AIUsageLog, DeepAnalysis, WebhookEvent


async def get_ai_usage_stats(session: AsyncSession, period: str = "day"):
    now = datetime.now()
    if period == "day":
        start_time = now - timedelta(days=1)
    elif period == "week":
        start_time = now - timedelta(days=7)
    elif period == "month":
        start_time = now - timedelta(days=30)
    elif period == "year":
        start_time = now - timedelta(days=365)
    else:
        start_time = now - timedelta(days=1)

    stmt_total = select(func.count(AIUsageLog.id)).select_from(AIUsageLog).filter(AIUsageLog.timestamp >= start_time)
    total_calls = await count_with_timeout(session, stmt_total) or 0

    stmt_route = (
        select(AIUsageLog.route_type, func.count(AIUsageLog.id).label("count"))
        .filter(AIUsageLog.timestamp >= start_time)
        .group_by(AIUsageLog.route_type)
    )
    res_route = await session.execute(stmt_route)
    route_stats = res_route.all()
    route_breakdown = {r.route_type: r.count for r in route_stats}
    if "reused" in route_breakdown:
        route_breakdown["reuse"] = route_breakdown.pop("reused")

    stmt_stats = select(
        func.sum(AIUsageLog.tokens_in).label("total_tokens_in"),
        func.sum(AIUsageLog.tokens_out).label("total_tokens_out"),
        func.sum(AIUsageLog.cost_estimate).label("total_cost"),
    ).filter(AIUsageLog.timestamp >= start_time)
    res_stats = await session.execute(stmt_stats)
    ai_stats = res_stats.first()

    stmt_cache_hits = (
        select(func.count(AIUsageLog.id))
        .select_from(AIUsageLog)
        .filter(AIUsageLog.timestamp >= start_time, AIUsageLog.cache_hit)
    )
    cache_hits_count = await count_with_timeout(session, stmt_cache_hits) or 0

    ai_calls = route_breakdown.get("ai", 0)
    avg_ai_cost = (
        float(ai_stats.total_cost or 0) / ai_calls
        if ai_calls > 0
        else float(Config.ai.AI_COST_PER_1K_INPUT_TOKENS * 0.5)
    )

    cache_calls = route_breakdown.get("cache", 0)
    rule_calls = route_breakdown.get("rule", 0)
    reuse_calls = route_breakdown.get("reuse", 0)
    cost_saved = (cache_calls + rule_calls + reuse_calls) * avg_ai_cost

    from core.redis_client import get_redis
    try:
        redis_client = get_redis()
        cache_keys = await redis_client.keys("analysis_*")
        active_keys = [k for k in cache_keys if not k.endswith(":hits")]
        active_cache_count = len(active_keys)

        total_hits = 0
        for key in active_keys:
            hits_val = await redis_client.get(f"{key}:hits")
            if hits_val:
                total_hits += int(hits_val)

        active_caches = (active_cache_count, total_hits)
    except Exception:
        active_caches = (0, 0)

    format_str = "%Y-%m-%d" if period in ("week", "month", "year") else "%H:00"
    stmt_all_logs = select(
        AIUsageLog.timestamp,
        AIUsageLog.route_type,
        AIUsageLog.tokens_in,
        AIUsageLog.tokens_out,
        AIUsageLog.cost_estimate,
    ).filter(AIUsageLog.timestamp >= start_time)
    res_all_logs = await session.execute(stmt_all_logs)
    all_logs = res_all_logs.all()

    trend_map = {}
    for row in all_logs:
        t = row.timestamp.strftime(format_str)
        if t not in trend_map:
            trend_map[t] = {
                "time": t,
                "total_calls": 0,
                "ai_calls": 0,
                "rule_calls": 0,
                "tokens": 0,
                "cost": 0.0,
            }
        trend_map[t]["total_calls"] += 1
        if row.route_type == "ai":
            trend_map[t]["ai_calls"] += 1
        elif row.route_type in ("rule", "cache", "reused"):
            trend_map[t]["rule_calls"] += 1
        trend_map[t]["tokens"] += (row.tokens_in or 0) + (row.tokens_out or 0)
        trend_map[t]["cost"] += float(row.cost_estimate or 0.0)

    trend_data = sorted(trend_map.values(), key=lambda x: x["time"])

    if total_calls > 0:
        percentages = {
            "ai": round(route_breakdown.get("ai", 0) / total_calls * 100, 1),
            "rule": round(route_breakdown.get("rule", 0) / total_calls * 100, 1),
            "cache": round(route_breakdown.get("cache", 0) / total_calls * 100, 1),
            "reuse": round(route_breakdown.get("reuse", 0) / total_calls * 100, 1),
        }
    else:
        percentages = {"ai": 0, "rule": 0, "cache": 0, "reuse": 0}

    active_cache_count = active_caches[0] if active_caches else 0
    total_cache_hits = active_caches[1] if active_caches else 0
    avg_hits = round(total_cache_hits / active_cache_count, 1) if active_cache_count > 0 else 0
    cache_saved = route_breakdown.get("cache", 0) + route_breakdown.get("rule", 0) + route_breakdown.get("reuse", 0)
    cache_hit_rate = (
        round((cache_hits_count) / (cache_hits_count + route_breakdown.get("ai", 0)) * 100, 1)
        if (cache_hits_count + route_breakdown.get("ai", 0)) > 0
        else 0
    )

    tokens_in = (ai_stats.total_tokens_in or 0) if ai_stats else 0
    tokens_out = (ai_stats.total_tokens_out or 0) if ai_stats else 0

    return {
        "total_calls": total_calls,
        "route_breakdown": route_breakdown,
        "percentages": percentages,
        "tokens": {
            "total": tokens_in + tokens_out,
            "input": tokens_in,
            "output": tokens_out,
        },
        "cost": {
            "total": float(ai_stats.total_cost or 0) if ai_stats else 0.0,
            "saved_estimate": cost_saved,
        },
        "cache_statistics": {
            "total_cache_entries": active_cache_count,
            "total_hits": total_cache_hits,
            "avg_hits_per_entry": avg_hits,
            "cache_hit_rate": cache_hit_rate,
            "saved_calls": cache_saved,
        },
        "trend": trend_data,
    }


async def get_deep_analysis_list(
    session: AsyncSession,
    page: int = 1,
    per_page: int = 20,
    cursor: int | None = None,
    status_filter: str = "",
    engine_filter: str = "",
    max_page: int = 500,
):
    per_page = max(1, min(per_page, 100))
    has_filters = bool(status_filter or engine_filter)

    if not has_filters:
        try:
            estimate_result = await session.execute(
                text("SELECT reltuples::bigint FROM pg_class WHERE relname = 'deep_analyses'")
            )
            estimate = estimate_result.scalar()
            if estimate is not None and estimate > 100000:
                total = int(estimate)
            else:
                total_query = select(func.count()).select_from(DeepAnalysis)
                total = await count_with_timeout(session, total_query)
        except Exception:
            total_query = select(func.count()).select_from(DeepAnalysis)
            total = await count_with_timeout(session, total_query)
    else:
        total_query = select(func.count()).select_from(DeepAnalysis)
        if status_filter:
            total_query = total_query.filter(DeepAnalysis.status == status_filter)
        if engine_filter:
            total_query = total_query.filter(DeepAnalysis.engine == engine_filter)
        total = await count_with_timeout(session, total_query)

    query = (
        select(
            DeepAnalysis,
            WebhookEvent.source,
            WebhookEvent.is_duplicate,
            WebhookEvent.beyond_window,
        )
        .outerjoin(WebhookEvent, WebhookEvent.id == DeepAnalysis.webhook_event_id)
        .order_by(DeepAnalysis.id.desc())
    )

    if cursor:
        query = query.filter(DeepAnalysis.id < cursor)
    if status_filter:
        query = query.filter(DeepAnalysis.status == status_filter)
    if engine_filter:
        query = query.filter(DeepAnalysis.engine == engine_filter)

    offset = 0
    if not cursor:
        if page > max_page:
            raise ValueError(f"page 超过上限 {max_page}，请使用 cursor 游标分页")
        offset = (page - 1) * per_page
        query = query.offset(offset)

    result = await session.execute(query.limit(per_page))
    rows = result.all()
    next_cursor = rows[-1][0].id if rows else None
    total_pages = math.ceil(total / per_page) if total is not None and total > 0 else (1 if total is not None else None)

    items = []
    for record, source, is_duplicate, beyond_window in rows:
        d = record.to_dict()
        d["source"] = source
        d["is_duplicate"] = bool(is_duplicate) if is_duplicate is not None else False
        d["beyond_window"] = bool(beyond_window) if beyond_window is not None else False
        items.append(d)

    return {
        "total": total,
        "total_pages": total_pages,
        "page": page if not cursor else None,
        "per_page": per_page,
        "next_cursor": next_cursor,
        "items": items,
    }


async def get_deep_analyses_for_webhook(session: AsyncSession, webhook_id: int):
    result = await session.execute(
        select(DeepAnalysis).filter_by(webhook_event_id=webhook_id).order_by(DeepAnalysis.created_at.desc())
    )
    return result.scalars().all()


async def analyze_webhook_with_ai(
    webhook_data: WebhookData, alert_hash: str | None = None, skip_cache: bool = False
) -> AnalysisResult:
    """
    使用 AI 分析 webhook 数据

    分析流程：
    1. 检查缓存（如果启用且 skip_cache=False）
    2. 智能路由判断（如果启用且 skip_cache=False）
    3. 调用 AI 分析（如果需要）
    4. 记录使用日志

    Args:
        webhook_data: Webhook 数据
        alert_hash: 告警哈希值（可选，未提供时自动生成）
        skip_cache: 是否跳过缓存，强制重新分析（默认 False）
    """
    source = webhook_data.get("source", "unknown")
    parsed_data = webhook_data.get("parsed_data", {})

    # 生成 alert_hash（如果未提供）
    if not alert_hash:
        from models import WebhookEvent

        alert_hash = WebhookEvent.generate_hash(parsed_data, source)

    # Step 1: 检查缓存（skip_cache=True 时跳过）
    if Config.ai.CACHE_ENABLED and not skip_cache:
        cached_result = await get_cached_analysis(alert_hash)
        if cached_result:
            logger.info(f"[Cache] 命中历史分析缓存: source={source}, hash={alert_hash[:16]}...")
            cached_result["_route_type"] = "cache"
            # 记录缓存命中
            await log_ai_usage(route_type="cache", alert_hash=alert_hash, source=source, cache_hit=True)
            # 返回缓存结果
            return cached_result
    elif skip_cache:
        logger.info(f"跳过缓存: 用户请求重新分析, source={source}")

    # Step 2: 检查是否启用 AI 分析
    if not policies.ai.ENABLE_AI_ANALYSIS:
        logger.info("AI 分析功能已禁用，使用基础规则分析")
        result = analyze_with_rules(parsed_data, source)
        result["_degraded"] = True
        result["_degraded_reason"] = "AI 分析功能已禁用"
        result["_route_type"] = "rule"
        await log_ai_usage(route_type="rule", alert_hash=alert_hash, source=source)
        # 返回结果
        return result

    # Step 3: 检查 API Key
    if not policies.ai.OPENAI_API_KEY:
        logger.warning("OpenAI API Key 未配置，降级为规则分析")
        result = analyze_with_rules(parsed_data, source)
        result["_degraded"] = True
        result["_degraded_reason"] = "OpenAI API Key 未配置"
        result["_route_type"] = "rule"
        # 发送降级通知
        await _send_degradation_alert(webhook_data, "OpenAI API Key 未配置")
        await log_ai_usage(route_type="rule", alert_hash=alert_hash, source=source)
        # 返回结果
        return result

    # Step 4: 调用 AI 分析
    try:
        analysis, tokens_in, tokens_out = await _call_ai_with_retry(parsed_data, source)

        analysis["_degraded"] = False
        analysis["_route_type"] = "ai"

        # 保存到缓存
        await save_to_cache(alert_hash, analysis)

        # 记录 AI 使用
        await log_ai_usage(
            route_type="ai",
            alert_hash=alert_hash,
            source=source,
            model=policies.ai.OPENAI_MODEL,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )

        return analysis

    except Exception as exc:
        logger.error(f"AI 分析在全部重试后依然失败: {exc!s}", exc_info=True)
        ai_error = exc

    # 根据配置决定是否降级
    if Config.ai.ENABLE_AI_DEGRADATION:
        logger.warning("启用 AI 降级策略，使用本地规则分析")
        result = analyze_with_rules(parsed_data, source)
        result["_degraded"] = True
        result["_degraded_reason"] = f"AI 分析失败: {ai_error!s}"
        result["_route_type"] = "rule"
        await _send_degradation_alert(webhook_data, str(ai_error))
        await log_ai_usage(route_type="rule", alert_hash=alert_hash, source=source)
        return result
    else:
        # 不降级，直接返回错误
        logger.error("AI 分析失败且未启用降级策略，返回错误")
        await _send_degradation_alert(webhook_data, str(ai_error))
        return {
            "summary": f"AI 分析失败: {ai_error!s}",
            "root_cause": "分析失败，请检查 AI 服务配置",
            "impact": "未知",
            "recommendations": ["检查 AI 服务连接", "查看日志获取详细信息"],
            "severity": "critical",
            "_degraded": True,
            "_degraded_reason": f"AI 分析失败: {ai_error!s}",
            "_route_type": "error",
        }


def analyze_with_rules(data: dict[str, Any], source: str) -> AnalysisResult:
    start_time = time.time()
    """基于规则的简单分析（AI 降级方案）"""
    # 基础分析结果
    analysis = {
        "source": source,
        "event_type": "unknown",
        "importance": "medium",
        "summary": "规则分析（AI 降级）",
        "actions": ["查看告警详情", "检查 AI 服务状态"],
        "risks": ["使用规则分析，可能不够准确"],
    }

    # 检测告警格式
    is_prometheus = "alerts" in data and isinstance(data.get("alerts"), list) and len(data.get("alerts", [])) > 0

    if is_prometheus:
        # Prometheus Alertmanager 格式
        first_alert = data["alerts"][0]
        labels = first_alert.get("labels", {})

        # 获取告警名称
        alert_name = labels.get("alertname", labels.get("alertingRuleName", "unknown"))
        analysis["event_type"] = alert_name

        # 获取告警级别
        alert_level = labels.get("internal_label_alert_level", labels.get("severity", "")).lower()

        # 判断重要性
        high_keywords = [k.strip().lower() for k in policies.ai.RULE_HIGH_KEYWORDS.split(",")]
        warn_keywords = [k.strip().lower() for k in policies.ai.RULE_WARN_KEYWORDS.split(",")]
        if alert_level in high_keywords:
            analysis["importance"] = "high"
            analysis["summary"] = f"🔴 严重告警: {alert_name}"
            analysis["actions"] = ["立即处理", "检查服务状态", "查看日志"]
        elif alert_level in warn_keywords:
            analysis["importance"] = "medium"
            analysis["summary"] = f"🟡 警告告警: {alert_name}"
            analysis["actions"] = ["关注趋势", "准备应对措施"]
        else:
            analysis["summary"] = f"📊 告警: {alert_name}"

    else:
        # 华为云/通用格式
        # 获取告警名称
        rule_name = data.get("RuleName") or data.get("alert_name") or data.get("MetricName", "unknown")
        analysis["event_type"] = rule_name

        # 获取告警级别
        level = str(data.get("Level", "")).lower()

        # 判断重要性
        high_keywords = [k.strip().lower() for k in policies.ai.RULE_HIGH_KEYWORDS.split(",")]
        warn_keywords = [k.strip().lower() for k in policies.ai.RULE_WARN_KEYWORDS.split(",")]
        if level in high_keywords:
            analysis["importance"] = "high"
            analysis["summary"] = f"🔴 严重告警: {rule_name}"
            analysis["actions"] = ["立即处理", "检查资源状态", "查看监控指标"]
        elif level in warn_keywords:
            analysis["importance"] = "medium"
            analysis["summary"] = f"🟡 警告告警: {rule_name}"
            analysis["actions"] = ["关注趋势", "评估影响范围"]
        else:
            # 检查指标名称中的关键词
            metric_name = str(data.get("MetricName", "")).lower()
            metric_keywords = [k.strip().lower() for k in policies.ai.RULE_METRIC_KEYWORDS.split(",")]
            if any(keyword in metric_name for keyword in metric_keywords):
                analysis["importance"] = "medium"
                analysis["summary"] = f"📊 监控告警: {rule_name}"
            else:
                analysis["summary"] = f"ℹ️ 通知: {rule_name}"

        # 检查阈值超标情况
        current_value = data.get("CurrentValue")
        threshold = data.get("Threshold")
        if current_value is not None and threshold is not None:
            try:
                current_num = float(current_value)
                threshold_num = float(threshold)
                if current_num > threshold_num * policies.ai.RULE_THRESHOLD_MULTIPLIER:
                    # 超过4倍阈值，提升重要性
                    analysis["importance"] = "high"
                    analysis["summary"] = f"🔴 严重超标: {rule_name} (当前值 {current_value} >> 阈值 {threshold})"
            except (ValueError, TypeError):
                pass

        # 检查资源信息
        resources = data.get("Resources", [])
        if resources and isinstance(resources, list):
            resource_count = len(resources)
            if resource_count > 1:
                analysis["impact_scope"] = f"影响 {resource_count} 个资源"

    # 通用事件类型检查（兜底）
    if analysis["event_type"] == "unknown":
        event = str(data.get("event", data.get("event_type", ""))).lower()
        if event:
            analysis["event_type"] = event

            # 基于关键词判断
            high_kw = [k.strip().lower() for k in policies.ai.RULE_HIGH_KEYWORDS.split(",")]
            warn_kw = [k.strip().lower() for k in policies.ai.RULE_WARN_KEYWORDS.split(",")]
            if any(keyword in event for keyword in high_kw):
                analysis["importance"] = "high"
                analysis["summary"] = f"🔴 严重事件: {event}"
            elif any(keyword in event for keyword in warn_kw):
                analysis["importance"] = "medium"
                analysis["summary"] = f"🟡 警告事件: {event}"

    duration = time.time() - start_time
    AI_ANALYSIS_DURATION_SECONDS.labels(source=sanitize_source(source), engine="rule").observe(duration)
    return analysis


async def _send_openclaw_failure_notification(webhook_data: WebhookData, source: str, error: str) -> None:
    """发送 OpenClaw 深度分析失败通知到飞书"""
    try:
        from adapters.ecosystem_adapters import send_feishu_deep_analysis
        from core.config import Config

        if not Config.ai.DEEP_ANALYSIS_FEISHU_WEBHOOK:
            return

        # 构造失败通知数据
        analysis_data = {
            "summary": "OpenClaw 深度分析触发失败",
            "root_cause": f"连续 3 次重试后仍失败: {error}",
            "impact": "无法获取深度根因分析结果",
            "recommendations": [
                "检查 OpenClaw 服务是否正常运行",
                f"检查网络连接: {Config.openclaw.OPENCLAW_GATEWAY_URL}",
                "查看服务端日志获取详细错误信息",
                "稍后手动重试深度分析",
            ],
            "confidence": 0,
            "status": "failed",
            "error": error,
        }

        event_id = webhook_data.get("id", "unknown")
        success = await send_feishu_deep_analysis(
            Config.ai.DEEP_ANALYSIS_FEISHU_WEBHOOK, analysis_data, source, event_id
        )
        if success:
            logger.info(f"OpenClaw 失败通知已发送到飞书: event_id={event_id}")
        else:
            try:
                from services.forward import record_failed_forward

                await record_failed_forward(
                    webhook_event_id=event_id if isinstance(event_id, int) else 0,
                    forward_rule_id=None,
                    target_url=Config.ai.DEEP_ANALYSIS_FEISHU_WEBHOOK,
                    target_type="feishu",
                    failure_reason="openclaw_failure_notification_failed",
                    error_message=f"OpenClaw 深度分析失败飞书通知发送失败: {error}",
                    forward_data={"event_id": event_id, "analysis_type": "openclaw_failure"},
                )
            except Exception as rec_err:
                logger.warning(f"记录飞书通知失败异常: {rec_err}")
    except Exception as e:
        logger.error(f"发送 OpenClaw 失败通知失败: {e}")

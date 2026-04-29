"""AI 分析编排层

作为主入口，协调缓存、AI 调用、规则降级等子模块，
提供 analyze_webhook_with_ai 和 analyze_with_rules 两个核心函数。
"""

import logging
import time
from typing import Any

from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from core.config import Config
from core.logger import logger
from core.metrics import AI_ANALYSIS_DURATION_SECONDS
from services.ai_cache import get_cached_analysis, log_ai_usage, save_to_cache
from services.ai_client import _send_degradation_alert, analyze_with_openai_tracked

# 类型别名
WebhookData = dict[str, Any]
AnalysisResult = dict[str, Any]
ForwardResult = dict[str, Any]


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential_jitter(initial=2, max=30, jitter=2),
    retry=retry_if_exception_type(Exception),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
async def _call_ai_with_retry(parsed_data: dict[str, Any], source: str) -> tuple[dict[str, Any], int, int]:
    """带指数退避重试的 AI 调用"""
    start_time = time.time()
    analysis, tokens_in, tokens_out = await analyze_with_openai_tracked(parsed_data, source)
    duration = time.time() - start_time
    AI_ANALYSIS_DURATION_SECONDS.labels(source=source, engine="openai").observe(duration)
    logger.info(f"AI 分析完成: {source}")
    return analysis, tokens_in, tokens_out


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
        from core.utils import generate_alert_hash

        alert_hash = generate_alert_hash(parsed_data, source)

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
    if not Config.ai.ENABLE_AI_ANALYSIS:
        logger.info("AI 分析功能已禁用，使用基础规则分析")
        result = analyze_with_rules(parsed_data, source)
        result["_degraded"] = True
        result["_degraded_reason"] = "AI 分析功能已禁用"
        result["_route_type"] = "rule"
        await log_ai_usage(route_type="rule", alert_hash=alert_hash, source=source)
        # 返回结果
        return result

    # Step 3: 检查 API Key
    if not Config.ai.OPENAI_API_KEY:
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
            model=Config.ai.OPENAI_MODEL,
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
        if alert_level in ["critical", "p0", "严重", "error"]:
            analysis["importance"] = "high"
            analysis["summary"] = f"🔴 严重告警: {alert_name}"
            analysis["actions"] = ["立即处理", "检查服务状态", "查看日志"]
        elif alert_level in ["warning", "warn", "p1"]:
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
        if level in ["critical", "error", "严重", "p0"]:
            analysis["importance"] = "high"
            analysis["summary"] = f"🔴 严重告警: {rule_name}"
            analysis["actions"] = ["立即处理", "检查资源状态", "查看监控指标"]
        elif level in ["warn", "warning", "p1"]:
            analysis["importance"] = "medium"
            analysis["summary"] = f"🟡 警告告警: {rule_name}"
            analysis["actions"] = ["关注趋势", "评估影响范围"]
        else:
            # 检查指标名称中的关键词
            metric_name = str(data.get("MetricName", "")).lower()
            if any(keyword in metric_name for keyword in ["4xxqps", "5xxqps", "error", "cpu", "memory", "disk"]):
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
                if current_num > threshold_num * 4:
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
            if any(keyword in event for keyword in ["error", "failure", "critical", "alert", "错误", "失败", "故障"]):
                analysis["importance"] = "high"
                analysis["summary"] = f"🔴 严重事件: {event}"
            elif any(keyword in event for keyword in ["warning", "warn", "警告"]):
                analysis["importance"] = "medium"
                analysis["summary"] = f"🟡 警告事件: {event}"

    duration = time.time() - start_time
    AI_ANALYSIS_DURATION_SECONDS.labels(source=source, engine="rule").observe(duration)
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
                from crud.webhook import record_failed_forward

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

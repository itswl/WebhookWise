"""AI analysis orchestrator."""

import math
import time
from typing import Any

import httpx

from contracts.webhook_payload import WebhookData
from core.app_context import get_config_manager
from core.logger import get_logger
from core.observability.metrics import (
    AI_ANALYSIS_DURATION_SECONDS,
    AI_DEGRADATIONS_TOTAL,
    AI_REQUESTS_TOTAL,
    sanitize_source,
)
from services.analysis import ai_llm_client as _llm_client
from services.analysis.ai_cache import get_cached_analysis, save_to_cache
from services.analysis.ai_prompt import (
    get_prompt_source,
    load_deep_analysis_prompt_template,
    load_user_prompt_template,
    reload_deep_analysis_prompt_template,
    reload_user_prompt_template,
)
from services.analysis.ai_usage import log_ai_usage
from services.analysis.alert_identity_context import build_alert_identity_context
from services.analysis.analysis_policies import AIProviderPolicy, RuleAnalysisPolicy
from services.analysis.resource_risk import apply_resource_importance_override
from services.dedup import generate_alert_hash
from services.webhooks.types import (
    AnalysisResult,
    cache_hit_count,
    is_analysis_degraded,
    mark_analysis_degraded,
    set_analysis_route,
)

logger = get_logger("analysis.ai_analyzer")

__all__ = [
    "analyze_webhook_with_ai",
    "analyze_with_rules",
    "get_prompt_source",
    "load_deep_analysis_prompt_template",
    "load_user_prompt_template",
    "log_ai_usage",
    "reload_deep_analysis_prompt_template",
    "reload_user_prompt_template",
]

_AI_POLICY_REFUSAL_MARKERS = (
    "terms of service",
    "content_policy",
    "content policy",
    "content filter",
    "prohibited",
    "policy violation",
    "violation of provider",
)


def _iter_exception_chain(root: BaseException) -> list[BaseException]:
    visited: set[int] = set()
    out: list[BaseException] = []
    curr: BaseException | None = root
    while curr is not None and id(curr) not in visited:
        visited.add(id(curr))
        out.append(curr)
        curr = curr.__cause__ or curr.__context__
    return out


def _extract_ai_error_message(exc: BaseException) -> str:
    for curr in _iter_exception_chain(exc):
        body = getattr(curr, "body", None)
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict):
                message = err.get("message")
                if isinstance(message, str) and message.strip():
                    return message.strip()
        text = str(curr).strip()
        if text:
            return text[:500]
    return type(exc).__name__


def _is_ai_policy_refusal(exc: BaseException) -> bool:
    for curr in _iter_exception_chain(exc):
        error_text = str(curr).lower()
        body = getattr(curr, "body", None)
        if isinstance(body, dict):
            error_text += f" {body!s}".lower()

        if any(marker in error_text for marker in _AI_POLICY_REFUSAL_MARKERS):
            return True

        status_code = getattr(curr, "status_code", None)
        if type(curr).__name__ == "PermissionDeniedError" and status_code == 403:
            return True

    return False


def analyze_with_rules(
    data: dict[str, Any], source: str, *, policy: RuleAnalysisPolicy | None = None
) -> AnalysisResult:
    policy = policy or RuleAnalysisPolicy.from_config()
    start_time = time.time()
    res: AnalysisResult = {
        "source": source,
        "event_type": "unknown",
        "importance": "medium",
        "summary": "规则分析（AI 降级）",
        "alert_identity": build_alert_identity_context(source, data).get("identity", {}),
        "actions": ["查看告警详情"],
        "risks": ["分析可能不准"],
    }

    rule_name = str(data.get("RuleName") or data.get("alert_name") or data.get("AlertName") or "unknown")
    res["event_type"] = rule_name

    labels = data.get("labels")
    labels_sev = labels.get("severity") if isinstance(labels, dict) else None
    level_raw = (
        data.get("Level") or data.get("level") or data.get("Severity") or data.get("severity") or labels_sev or ""
    )
    level = str(level_raw).strip().lower()
    name_l = rule_name.lower()

    high_kw = policy.high_keywords
    warn_kw = policy.warning_keywords
    metric_kw = policy.metric_keywords

    importance = "medium"
    if level in high_kw or any(k in level for k in high_kw) or any(k in name_l for k in high_kw):
        importance = "high"
    elif level in warn_kw or any(k in level for k in warn_kw) or any(k in name_l for k in warn_kw):
        importance = "medium"
    elif any(k in level for k in ("info", "information", "notice", "ok", "resolved", "success", "normal", "恢复")):
        importance = "low"

    cur_val = data.get("CurrentValue") or data.get("current_value") or data.get("current") or data.get("value")
    thr_val = data.get("Threshold") or data.get("threshold") or data.get("limit")
    multiplier = policy.threshold_multiplier

    def _record_numeric_parse_failure(field: str, value: Any, reason: str) -> None:
        logger.debug(
            "[AI] 规则分析数值字段解析失败 source=%s field=%s reason=%s value=%r",
            source,
            field,
            reason,
            value,
        )

    def _to_float(v: Any, field: str) -> float | None:
        if v is None:
            return None
        if isinstance(v, (int, float)):
            numeric = float(v)
            if math.isfinite(numeric):
                return numeric
            _record_numeric_parse_failure(field, v, "non_finite")
            return None
        s = str(v).strip()
        if not s:
            return None
        try:
            numeric = float(s)
        except ValueError:
            _record_numeric_parse_failure(field, v, "non_numeric")
            return None
        if math.isfinite(numeric):
            return numeric
        _record_numeric_parse_failure(field, v, "non_finite")
        return None

    cur_f = _to_float(cur_val, "current")
    thr_f = _to_float(thr_val, "threshold")
    if cur_f is not None and thr_f is not None and thr_f > 0:
        data_l = str(data).lower()
        is_metric_related = any(k in name_l for k in metric_kw) or any(k in data_l for k in metric_kw)
        if is_metric_related:
            if cur_f >= thr_f * multiplier:
                importance = "high"
            elif cur_f >= thr_f and importance != "high":
                importance = "medium"

    res["importance"] = importance
    prefix = {"high": "🔴", "medium": "🟠", "low": "🟢"}.get(importance, "🟠")
    if cur_f is not None and thr_f is not None:
        res["summary"] = f"{prefix} {rule_name}: 当前值 {cur_f:g} / 阈值 {thr_f:g}"
    else:
        res["summary"] = f"{prefix} {rule_name}"

    if importance == "high":
        res["actions"] = ["立即确认影响范围", "检查近 5 分钟指标/日志", "按 Runbook 执行处置"]
        res["risks"] = ["可能导致服务不可用或核心能力下降", "可能影响用户或业务数据"]
    elif importance == "low":
        res["actions"] = ["确认是否为预期事件", "必要时补充告警规则"]
        res["risks"] = ["告警可能噪声偏多"]

    res = apply_resource_importance_override(res, data)

    metric_source = sanitize_source(source)
    AI_REQUESTS_TOTAL.labels(metric_source, "rule", "success").inc()
    AI_ANALYSIS_DURATION_SECONDS.labels(source=metric_source, engine="rule").observe(time.time() - start_time)
    return res


async def _send_ai_error_alert(webhook_data: WebhookData, error_reason: str, is_degraded: bool = False) -> None:
    from services.operations.ai_error_notifications import send_ai_error_alert

    await send_ai_error_alert(webhook_data, error_reason, is_degraded=is_degraded)


async def _degrade_to_rules(
    webhook_data: WebhookData,
    parsed: dict[str, Any],
    source: str,
    alert_hash: str,
    reason: str,
    *,
    notify: bool,
) -> AnalysisResult:
    logger.info("[AI] 降级为规则分析 source=%s reason=%s", source, reason)
    AI_DEGRADATIONS_TOTAL.labels(str(reason).split(":", 1)[0][:80] or "unknown").inc()
    res = mark_analysis_degraded(analyze_with_rules(parsed, source), reason, route="rule")
    await log_ai_usage("rule", alert_hash, source)
    if notify:
        await _send_ai_error_alert(webhook_data, reason, is_degraded=True)
    return res


async def analyze_webhook_with_ai(
    webhook_data: WebhookData,
    alert_hash: str | None = None,
    skip_cache: bool = False,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> AnalysisResult:
    ai_config = get_config_manager().ai
    cache_enabled = bool(ai_config.CACHE_ENABLED)
    cache_ttl_seconds = int(ai_config.ANALYSIS_CACHE_TTL)
    provider_policy = AIProviderPolicy.from_config()
    source, parsed = webhook_data.get("source", "unknown"), webhook_data.get("parsed_data", {})
    if not alert_hash:
        alert_hash = generate_alert_hash(parsed, source)

    if cache_enabled and not skip_cache:
        cached = await get_cached_analysis(alert_hash, enabled=cache_enabled, ttl_seconds=cache_ttl_seconds)
        if cached:
            hits = cache_hit_count(cached)
            logger.info("[AI] Redis 缓存命中 source=%s hits=%s hash=%s...", source, hits, alert_hash[:12])
            await log_ai_usage("cache", alert_hash, source, cache_hit=True, policy=provider_policy)
            cached_result = apply_resource_importance_override(cached.copy(), parsed)
            set_analysis_route(cached_result, "cache")
            AI_REQUESTS_TOTAL.labels(sanitize_source(source), "cache", "hit").inc()
            return cached_result

    if not provider_policy.available:
        reason = "disabled" if not provider_policy.enabled else "no_api_key"
        notify = provider_policy.degradation_enabled and reason == "no_api_key"
        notify_reason = (
            "配置了开启 AI 分析，但当前 Worker 进程未能读取到 OPENAI_API_KEY，已自动降级为规则分析。"
            if notify
            else reason
        )

        return await _degrade_to_rules(webhook_data, parsed, source, alert_hash, notify_reason, notify=notify)

    try:
        logger.debug(
            "[AI] 发起 OpenAI 请求 source=%s model=%s hash=%s...",
            source,
            provider_policy.model,
            alert_hash[:12],
        )
        analysis, t_in, t_out = await _llm_client._call_ai_with_retry(parsed, source, http_client=http_client)
        logger.info(
            "[AI] 分析完成 source=%s model=%s tokens_in=%d tokens_out=%d importance=%s",
            source,
            provider_policy.model,
            t_in,
            t_out,
            str(analysis.get("importance", "unknown")).lower().rsplit(".", 1)[-1],
        )
        analysis_result = apply_resource_importance_override(analysis.copy(), parsed)
        if not is_analysis_degraded(analysis_result):
            await save_to_cache(
                alert_hash,
                analysis_result.copy(),
                enabled=cache_enabled,
                ttl_seconds=cache_ttl_seconds,
            )
        await log_ai_usage(
            "ai",
            alert_hash,
            source,
            model=provider_policy.model,
            tokens_in=t_in,
            tokens_out=t_out,
            policy=provider_policy,
        )
        set_analysis_route(analysis_result, "ai")
        return analysis_result
    except Exception as e:
        error_reason = _extract_ai_error_message(e)
        logger.error("[AI] 分析失败 source=%s error_type=%s error=%s", source, type(e).__name__, error_reason)
        if _is_ai_policy_refusal(e):
            return await _degrade_to_rules(
                webhook_data,
                parsed,
                source,
                alert_hash,
                f"llm_policy_refusal: {error_reason}",
                notify=True,
            )

        if provider_policy.degradation_enabled:
            return await _degrade_to_rules(
                webhook_data,
                parsed,
                source,
                alert_hash,
                f"ai_error: {error_reason}",
                notify=True,
            )
        await _send_ai_error_alert(webhook_data, error_reason, is_degraded=False)
        raise

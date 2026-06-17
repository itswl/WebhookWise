"""AI analysis orchestrator."""

import math
import time
from typing import Any

import httpx

from contracts.webhook_payload import WebhookData
from core.app_context import get_config_manager
from core.circuit_breaker import CircuitBreakerOpenException
from core.logger import get_logger
from core.observability.metrics import (
    AI_ANALYSIS_DURATION_SECONDS,
    AI_DEGRADATIONS_TOTAL,
    AI_REQUESTS_TOTAL,
    sanitize_source,
)
from services.analysis import ai_llm_client as _llm_client
from services.analysis.ai_cache import get_cached_analysis, save_to_cache
from services.analysis.ai_errors import (
    extract_ai_error_message as _extract_ai_error_message,
)
from services.analysis.ai_errors import (
    is_ai_policy_refusal as _is_ai_policy_refusal,
)
from services.analysis.ai_errors import (
    is_ai_provider_runtime_error as _is_ai_provider_runtime_error,
)
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

_IMPORTANCE_EMOJI = {"high": "🔴", "medium": "🟠", "low": "🟢"}
_IMPORTANCE_EMOJI_DEFAULT = "🟠"

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

def analyze_with_rules(
    data: dict[str, Any], source: str, *, policy: RuleAnalysisPolicy | None = None
) -> AnalysisResult:
    policy = policy or RuleAnalysisPolicy.from_config()
    start_time = time.time()
    res: AnalysisResult = {
        "source": source,
        "event_type": "unknown",
        "importance": "medium",
        "summary": "Rule-based analysis (AI degraded)",
        "alert_identity": build_alert_identity_context(source, data).get("identity", {}),
        "actions": ["View alert details"],
        "risks": ["Analysis may be inaccurate"],
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
            "[AI] Rule analysis failed to parse numeric field source=%s field=%s reason=%s value=%r",
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
    prefix = _IMPORTANCE_EMOJI.get(importance, _IMPORTANCE_EMOJI_DEFAULT)
    if cur_f is not None and thr_f is not None:
        res["summary"] = f"{prefix} {rule_name}: current {cur_f:g} / threshold {thr_f:g}"
    else:
        res["summary"] = f"{prefix} {rule_name}"

    if importance == "high":
        res["actions"] = ["Immediately confirm the scope of impact", "Check metrics/logs from the last 5 minutes", "Follow the runbook to remediate"]
        res["risks"] = ["May cause service unavailability or degradation of core capabilities", "May affect users or business data"]
    elif importance == "low":
        res["actions"] = ["Confirm whether this is an expected event", "Supplement alerting rules if necessary"]
        res["risks"] = ["The alert may be largely noise"]

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
    logger.info("[AI] Degrading to rule-based analysis source=%s reason=%s", source, reason)
    AI_DEGRADATIONS_TOTAL.labels(str(reason).split(":", 1)[0][:80] or "unknown").inc()
    res = mark_analysis_degraded(analyze_with_rules(parsed, source), reason, route="rule")
    await log_ai_usage("rule", alert_hash, source)
    if notify:
        await _send_ai_error_alert(webhook_data, reason, is_degraded=True)
    return res


def _routing_skip_importances(ai_config: Any) -> frozenset[str]:
    raw = str(getattr(ai_config, "AI_ROUTING_SKIP_IMPORTANCE", "") or "")
    return frozenset(p.strip().lower() for p in raw.split(",") if p.strip())


async def _maybe_route_to_rules(
    webhook_data: WebhookData,
    parsed: dict[str, Any],
    source: str,
    alert_hash: str,
    ai_config: Any,
) -> AnalysisResult | None:
    """Return a rule-only analysis (skipping the LLM) when tiered routing is on
    and the rule pass judges the alert low-value; else None (proceed to AI)."""
    if not bool(getattr(ai_config, "AI_ROUTING_ENABLED", False)):
        return None
    skip = _routing_skip_importances(ai_config)
    if not skip:
        return None
    res = apply_resource_importance_override(analyze_with_rules(parsed, source), parsed)
    importance = str(res.get("importance", "")).lower()
    if importance not in skip:
        return None
    logger.info("[AI] Tiered routing: low-value alert skips LLM source=%s importance=%s", source, importance)
    await log_ai_usage("rule_routed", alert_hash, source)
    AI_REQUESTS_TOTAL.labels(sanitize_source(source), "rule_routed", "success").inc()
    return set_analysis_route(res, "rule_routed")


async def analyze_webhook_with_ai(
    webhook_data: WebhookData,
    alert_hash: str | None = None,
    skip_cache: bool = False,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> AnalysisResult:
    ai_config = get_config_manager().ai
    cache_enabled = bool(ai_config.CACHE_ENABLED)
    cache_ttl_seconds = int(ai_config.ANALYSIS_CACHE_TTL_SECONDS)
    provider_policy = AIProviderPolicy.from_config()
    source, parsed = webhook_data.get("source", "unknown"), webhook_data.get("parsed_data", {})
    if not alert_hash:
        alert_hash = generate_alert_hash(parsed, source)

    if cache_enabled and not skip_cache:
        cached = await get_cached_analysis(alert_hash, enabled=cache_enabled, ttl_seconds=cache_ttl_seconds)
        if cached:
            hits = cache_hit_count(cached)
            logger.info("[AI] Redis cache hit source=%s hits=%s hash=%s...", source, hits, alert_hash[:12])
            await log_ai_usage("cache", alert_hash, source, cache_hit=True, policy=provider_policy)
            cached_result = apply_resource_importance_override(cached.copy(), parsed)
            set_analysis_route(cached_result, "cache")
            AI_REQUESTS_TOTAL.labels(sanitize_source(source), "cache", "hit").inc()
            return cached_result

    # Tiered routing (opt-in): if the cheap rule pass deems this a low-value alert,
    # skip the paid LLM and return the rule analysis. This is an intentional route,
    # NOT a degradation — so it is logged as "rule_routed" and not marked degraded.
    routed = await _maybe_route_to_rules(webhook_data, parsed, source, alert_hash, ai_config)
    if routed is not None:
        return routed

    if not provider_policy.available:
        reason = "disabled" if not provider_policy.enabled else "no_api_key"
        notify = provider_policy.degradation_enabled and reason == "no_api_key"
        notify_reason = (
            "AI analysis is enabled in the configuration, but the current Worker process could not read OPENAI_API_KEY, so it has automatically degraded to rule-based analysis."
            if notify
            else reason
        )

        return await _degrade_to_rules(webhook_data, parsed, source, alert_hash, notify_reason, notify=notify)

    try:
        logger.debug(
            "[AI] Sending OpenAI request source=%s model=%s hash=%s...",
            source,
            provider_policy.model,
            alert_hash[:12],
        )
        analysis, t_in, t_out = await _llm_client.call_ai_with_breaker(parsed, source, http_client=http_client)
        logger.info(
            "[AI] Analysis complete source=%s model=%s tokens_in=%d tokens_out=%d importance=%s",
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
    except CircuitBreakerOpenException:
        # Provider is broadly failing; skip the retry budget and degrade now.
        logger.warning("[AI] LLM circuit breaker is open, degrading directly to rule-based analysis source=%s", source)
        return await _degrade_to_rules(
            webhook_data,
            parsed,
            source,
            alert_hash,
            "llm_circuit_open",
            notify=False,
        )
    except Exception as e:
        if _is_ai_policy_refusal(e):
            error_reason = _extract_ai_error_message(e)
            logger.warning(
                "[AI] Policy refusal, degrading to rule-based analysis source=%s error_type=%s error=%s",
                source,
                type(e).__name__,
                error_reason,
            )
            return await _degrade_to_rules(
                webhook_data,
                parsed,
                source,
                alert_hash,
                f"llm_policy_refusal: {error_reason}",
                notify=True,
            )

        if not _is_ai_provider_runtime_error(e):
            raise

        error_reason = _extract_ai_error_message(e)
        logger.error("[AI] Analysis failed source=%s error_type=%s error=%s", source, type(e).__name__, error_reason)
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

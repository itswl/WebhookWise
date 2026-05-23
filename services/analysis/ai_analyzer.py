"""AI analysis orchestrator."""

from typing import Any

import httpx

from core.app_context import get_config_manager
from core.logger import get_logger
from core.observability.metrics import AI_DEGRADATIONS_TOTAL, ALERT_NUMERIC_PARSE_FAILURE_TOTAL
from services.analysis import ai_llm_client as _llm_client
from services.analysis.ai_cache import get_cache_key, get_cached_analysis, save_to_cache
from services.analysis.ai_prompt import (
    get_prompt_source,
    load_deep_analysis_prompt_template,
    load_user_prompt_template,
    reload_deep_analysis_prompt_template,
    reload_user_prompt_template,
)
from services.analysis.ai_usage import log_ai_usage
from services.analysis.analysis_policies import AIProviderPolicy, RuleAnalysisPolicy
from services.analysis.analysis_queries import (
    get_ai_usage_stats,
    get_deep_analyses_for_webhook,
    get_deep_analysis_list,
)
from services.analysis.rule_analyzer import analyze_with_rules as _analyze_with_rules
from services.dedup import generate_alert_hash
from services.webhooks.types import AnalysisResult, WebhookData

logger = get_logger("analysis.ai_analyzer")

__all__ = [
    "analyze_webhook_with_ai",
    "analyze_with_rules",
    "get_cache_key",
    "get_ai_usage_stats",
    "get_cached_analysis",
    "get_deep_analyses_for_webhook",
    "get_deep_analysis_list",
    "get_prompt_source",
    "initialize_openai_client",
    "load_deep_analysis_prompt_template",
    "load_user_prompt_template",
    "log_ai_usage",
    "reload_deep_analysis_prompt_template",
    "reload_user_prompt_template",
    "reset_openai_client",
    "save_to_cache",
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


async def _get_instructor_client_async(*, http_client: httpx.AsyncClient | None = None) -> Any:
    return await _llm_client._get_instructor_client_async(http_client=http_client)


async def initialize_openai_client(
    policy: AIProviderPolicy | None = None, *, http_client: httpx.AsyncClient | None = None
) -> None:
    await _llm_client.initialize_openai_client(policy=policy, http_client=http_client)


async def reset_openai_client() -> None:
    await _llm_client.reset_openai_client()


async def _call_ai_with_retry(
    parsed_data: dict[str, Any], source: str, *, http_client: httpx.AsyncClient | None = None
) -> tuple[AnalysisResult, int, int]:
    return await _llm_client._call_ai_with_retry(parsed_data, source, http_client=http_client)


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
    return _analyze_with_rules(
        data,
        source,
        policy=policy,
        numeric_parse_failure_counter=ALERT_NUMERIC_PARSE_FAILURE_TOTAL,
    )


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
    res = analyze_with_rules(parsed, source)
    res["_degraded"] = True
    res["_route_type"] = "rule"
    res["_degraded_reason"] = reason
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
            hits = cached.get("_cache_hit_count", 1)
            logger.info("[AI] Redis 缓存命中 source=%s hits=%s hash=%s...", source, hits, alert_hash[:12])
            await log_ai_usage("cache", alert_hash, source, cache_hit=True, policy=provider_policy)
            cached_result = cached.copy()
            cached_result["_route_type"] = "cache"
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
        if http_client is not None:
            analysis, t_in, t_out = await _call_ai_with_retry(parsed, source, http_client=http_client)
        else:
            analysis, t_in, t_out = await _call_ai_with_retry(parsed, source)
        logger.info(
            "[AI] 分析完成 source=%s model=%s tokens_in=%d tokens_out=%d importance=%s",
            source,
            provider_policy.model,
            t_in,
            t_out,
            str(analysis.get("importance", "unknown")).lower().rsplit(".", 1)[-1],
        )
        if not analysis.get("_degraded"):
            await save_to_cache(alert_hash, analysis, enabled=cache_enabled, ttl_seconds=cache_ttl_seconds)
        await log_ai_usage(
            "ai",
            alert_hash,
            source,
            model=provider_policy.model,
            tokens_in=t_in,
            tokens_out=t_out,
            policy=provider_policy,
        )
        analysis_result = analysis.copy()
        analysis_result["_route_type"] = "ai"
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

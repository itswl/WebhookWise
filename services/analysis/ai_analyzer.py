"""AI analysis orchestrator.

This module intentionally remains the public compatibility facade for callers
that import analysis helpers from ``services.analysis.ai_analyzer``.
"""

import asyncio
import time
from typing import Any

import httpx

from core.logger import logger, mask_url
from core.metrics import ALERT_NUMERIC_PARSE_FAILURE_TOTAL
from models import WebhookEvent
from services.analysis import ai_llm_client as _llm_client
from services.analysis.ai_cache import get_cache_key, get_cached_analysis, save_to_cache
from services.analysis.ai_policies import AICachePolicy, AIProviderPolicy, RuleAnalysisPolicy
from services.analysis.ai_prompt import (
    _resolve_prompt_path,
    get_prompt_source,
    load_user_prompt_template,
    reload_user_prompt_template,
)
from services.analysis.ai_usage import log_ai_usage
from services.analysis.analysis_queries import (
    get_ai_usage_stats,
    get_deep_analyses_for_webhook,
    get_deep_analysis_list,
)
from services.analysis.rule_analyzer import analyze_with_rules as _analyze_with_rules
from services.runtime_config.runtime_access import (
    RuntimeConfigRefreshPolicy,
    format_runtime_meta_time,
    get_runtime_config_meta,
    get_runtime_config_source,
    reload_runtime_config,
)
from services.webhooks.types import AnalysisResult, WebhookData

__all__ = [
    "_call_ai_with_retry",
    "_get_instructor_client",
    "_get_instructor_client_async",
    "_resolve_prompt_path",
    "analyze_webhook_with_ai",
    "analyze_with_rules",
    "get_cache_key",
    "get_ai_usage_stats",
    "get_cached_analysis",
    "get_deep_analyses_for_webhook",
    "get_deep_analysis_list",
    "get_prompt_source",
    "initialize_openai_client",
    "load_user_prompt_template",
    "log_ai_usage",
    "reload_user_prompt_template",
    "reset_openai_client",
    "save_to_cache",
]

_last_policy_refresh_at: float = 0.0
_policy_refresh_lock = asyncio.Lock()

_AI_POLICY_REFUSAL_MARKERS = (
    "terms of service",
    "content_policy",
    "content policy",
    "content filter",
    "prohibited",
    "policy violation",
    "violation of provider",
)


def __getattr__(name: str) -> Any:
    if name in {"_openai_client", "_instructor_client"}:
        return getattr(_llm_client, name)
    raise AttributeError(name)


def _get_instructor_client() -> Any:
    return _llm_client._get_instructor_client()


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
) -> tuple[dict[str, Any], int, int]:
    return await _llm_client._call_ai_with_retry(parsed_data, source, http_client=http_client)


async def _maybe_refresh_runtime_policies(keys: tuple[str, ...], min_interval_seconds: int = 30) -> None:
    global _last_policy_refresh_at
    if not RuntimeConfigRefreshPolicy.from_config().enabled:
        return
    async with _policy_refresh_lock:
        now = time.time()
        if now - _last_policy_refresh_at < min_interval_seconds:
            return
        before_policy = AIProviderPolicy.from_config()
        await reload_runtime_config()
        after_policy = AIProviderPolicy.from_config()
        api_url_changed = before_policy.api_url != after_policy.api_url
        api_key_changed = before_policy.api_key != after_policy.api_key
        if api_url_changed or api_key_changed:
            logger.info(
                "[AI] 运行时配置刷新触发客户端重建 changed_api_url=%s changed_api_key=%s old_api_url=%s new_api_url=%s keys=%s",
                api_url_changed,
                api_key_changed,
                mask_url(before_policy.api_url),
                mask_url(after_policy.api_url),
                ",".join(keys),
            )
            await reset_openai_client()
        else:
            logger.debug("[AI] 运行时配置刷新完成 keys=%s", ",".join(keys))
        _last_policy_refresh_at = now


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
    res = analyze_with_rules(parsed, source)
    res.update({"_degraded": True, "_route_type": "rule", "_degraded_reason": reason})
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
    await _maybe_refresh_runtime_policies(("OPENAI_MODEL", "OPENAI_API_KEY", "OPENAI_API_URL"), min_interval_seconds=60)
    cache_policy = AICachePolicy.from_config()
    provider_policy = AIProviderPolicy.from_config()
    source, parsed = webhook_data.get("source", "unknown"), webhook_data.get("parsed_data", {})
    if not alert_hash:
        alert_hash = WebhookEvent.generate_hash(parsed, source)

    if cache_policy.enabled and not skip_cache:
        cached = await get_cached_analysis(alert_hash, policy=cache_policy)
        if cached:
            hits = cached.get("_cache_hit_count", 1)
            logger.info("[AI] Redis 缓存命中 source=%s hits=%s hash=%s...", source, hits, alert_hash[:12])
            await log_ai_usage("cache", alert_hash, source, cache_hit=True, policy=provider_policy)
            return {**cached, "_route_type": "cache"}

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
        model_meta = get_runtime_config_meta("OPENAI_MODEL")
        logger.debug(
            "[AI] 发起 OpenAI 请求 source=%s model=%s model_source=%s model_updated_at=%s hash=%s...",
            source,
            provider_policy.model,
            get_runtime_config_source("OPENAI_MODEL"),
            format_runtime_meta_time(model_meta.get("updated_at")),
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
            await save_to_cache(alert_hash, analysis, policy=cache_policy)
        await log_ai_usage(
            "ai",
            alert_hash,
            source,
            model=provider_policy.model,
            tokens_in=t_in,
            tokens_out=t_out,
            policy=provider_policy,
        )
        return {**analysis, "_route_type": "ai"}
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


async def _send_openclaw_failure_notification(webhook_data: WebhookData, source: str, error: str) -> None:
    from services.operations.deep_analysis_notifications import send_deep_analysis_failure_notification

    await send_deep_analysis_failure_notification(
        {
            "id": webhook_data.get("id", 0) or 0,
            "webhook_event_id": int(webhook_data.get("id", 0) or 0),
            "engine": "openclaw",
            "analysis_result": {"root_cause": error, "impact": "分析失败，无法评估影响范围"},
            "duration_seconds": 0,
            "source": source,
        },
        error,
    )

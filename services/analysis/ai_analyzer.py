"""AI 分析核心引擎

集成 Prompt 管理、缓存、LLM 调用、结构化解析与成本追踪。
"""

import asyncio
import logging
import math
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol, cast

import httpx
import instructor
import orjson
import yaml
from openai import AsyncOpenAI
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from core.http_client import get_http_client
from core.logger import logger
from core.metrics import (
    AI_ANALYSIS_DURATION_SECONDS,
    AI_COST_USD_TOTAL,
    AI_TOKENS_TOTAL,
    ALERT_NUMERIC_PARSE_FAILURE_TOTAL,
    OPENAI_ERRORS_TOTAL,
    sanitize_source,
)
from core.otel import span as otel_span
from db.session import session_scope
from models import AIUsageLog, WebhookEvent
from schemas import WebhookAnalysisResult
from services.analysis.ai_policies import AICachePolicy, AIPromptPolicy, AIProviderPolicy, RuleAnalysisPolicy
from services.analysis.analysis_queries import (
    get_ai_usage_stats,
    get_deep_analyses_for_webhook,
    get_deep_analysis_list,
)
from services.runtime_config.runtime_access import (
    RuntimeConfigRefreshPolicy,
    format_runtime_meta_time,
    get_runtime_config_meta,
    get_runtime_config_source,
    reload_runtime_config,
)
from services.webhooks.payload_sanitizer import sanitize_for_ai_async
from services.webhooks.types import AnalysisResult, WebhookData

__all__ = [
    "analyze_webhook_with_ai",
    "analyze_with_rules",
    "get_ai_usage_stats",
    "get_cached_analysis",
    "get_deep_analyses_for_webhook",
    "get_deep_analysis_list",
    "initialize_openai_client",
    "log_ai_usage",
    "reset_openai_client",
    "save_to_cache",
]

_last_policy_refresh_at: float = 0.0
_policy_refresh_lock = asyncio.Lock()
_prompt_template_lock = asyncio.Lock()
_openai_client_lock = asyncio.Lock()

_AI_POLICY_REFUSAL_MARKERS = (
    "terms of service",
    "content_policy",
    "content policy",
    "content filter",
    "prohibited",
    "policy violation",
    "violation of provider",
)


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
        if before_policy.api_url != after_policy.api_url or before_policy.api_key != after_policy.api_key:
            await reset_openai_client()
        _last_policy_refresh_at = now


# ── Prompt 管理 ─────────────────────────────────────────────────────────────

_user_prompt_template: str | None = None
_user_prompt_source: str = "unknown"


def get_prompt_source() -> str:
    return _user_prompt_source


def _resolve_prompt_path(prompt_file: str) -> Path:
    file_path = Path(prompt_file)
    if file_path.is_absolute():
        return file_path
    project_root = Path(__file__).resolve().parents[2]
    return project_root / file_path


async def load_user_prompt_template(policy: AIPromptPolicy | None = None) -> str:
    global _user_prompt_template, _user_prompt_source
    policy = policy or AIPromptPolicy.from_config()
    async with _prompt_template_lock:
        if _user_prompt_template is not None:
            return _user_prompt_template

        if policy.inline_prompt:
            _user_prompt_source, _user_prompt_template = "env:AI_USER_PROMPT", policy.inline_prompt
            return _user_prompt_template

        prompt_file = policy.prompt_file
        if prompt_file:
            file_path = _resolve_prompt_path(prompt_file)
            if file_path.exists():
                try:
                    with open(file_path, encoding="utf-8") as f:
                        _user_prompt_template = f.read()
                    _user_prompt_source = f"file:{file_path}"
                    return _user_prompt_template
                except Exception as e:
                    logger.warning("从文件加载 prompt 模板失败: %s", e)

        _user_prompt_source = "builtin:default"
        _user_prompt_template = policy.builtin_prompt
        return _user_prompt_template


async def reload_user_prompt_template(policy: AIPromptPolicy | None = None) -> str:
    global _user_prompt_template
    async with _prompt_template_lock:
        _user_prompt_template = None
    return await load_user_prompt_template(policy=policy)


# ── 缓存管理 ─────────────────────────────────────────────────────────────


def get_cache_key(alert_hash: str) -> str:
    return f"analysis_{alert_hash}"


async def get_cached_analysis(alert_hash: str, *, policy: AICachePolicy | None = None) -> AnalysisResult | None:
    policy = policy or AICachePolicy.from_config()
    if not policy.enabled:
        return None
    try:
        from core.redis_client import redis_get_str, redis_incr_with_expire

        ck = get_cache_key(alert_hash)
        cached_json = await redis_get_str(ck)
        if not cached_json:
            return None
        parsed = orjson.loads(cached_json)
        if not isinstance(parsed, dict):
            return None
        res: AnalysisResult = dict(parsed)
        counter_key = f"{ck}:hits"
        hits = await redis_incr_with_expire(counter_key, policy.ttl_seconds)
        res.update({"_cache_hit": True, "_cache_hit_count": hits})
        return res
    except Exception as e:
        logger.warning("读取缓存失败: %s", e)
        return None


async def save_to_cache(
    alert_hash: str, analysis_result: AnalysisResult, *, policy: AICachePolicy | None = None
) -> bool:
    policy = policy or AICachePolicy.from_config()
    if not policy.enabled:
        return False
    try:
        from core.redis_client import redis_setex_bytes, redis_setex_str

        ck = get_cache_key(alert_hash)
        res_to_cache = {k: v for k, v in analysis_result.items() if not k.startswith("_")}
        cached_bytes = orjson.dumps(res_to_cache)
        counter_key = f"{ck}:hits"
        await redis_setex_bytes(ck, policy.ttl_seconds, cached_bytes)
        await redis_setex_str(counter_key, policy.ttl_seconds, "0")
        return True
    except Exception as e:
        logger.warning("保存缓存失败: %s", e)
        return False


async def log_ai_usage(
    route_type: str,
    alert_hash: str,
    source: str,
    model: str | None = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cache_hit: bool = False,
    policy: AIProviderPolicy | None = None,
) -> None:
    try:
        policy = policy or AIProviderPolicy.from_config()
        cost = 0.0
        if route_type == "ai" and tokens_in > 0:
            cost = policy.cost_for_tokens(tokens_in, tokens_out)
        async with session_scope() as session:
            session.add(
                AIUsageLog(
                    model=model or policy.model,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    cost_estimate=cost,
                    cache_hit=cache_hit,
                    route_type=route_type,
                    alert_hash=alert_hash,
                    source=source,
                )
            )
    except Exception as e:
        logger.warning("记录 AI 使用日志失败: %s", e)


# ── LLM 调用 ─────────────────────────────────────────────────────────────

_openai_client: AsyncOpenAI | None = None
_instructor_client: instructor.Instructor | None = None


class _CompletionUsage(Protocol):
    prompt_tokens: int
    completion_tokens: int


class _Completion(Protocol):
    usage: _CompletionUsage | None


class _InstructorCompletions(Protocol):
    async def create_with_completion(
        self,
        *,
        model: str,
        response_model: type[WebhookAnalysisResult],
        messages: Sequence[dict[str, str]],
        temperature: float,
        max_retries: int,
    ) -> tuple[WebhookAnalysisResult, _Completion]: ...


class _InstructorChat(Protocol):
    completions: _InstructorCompletions


class _InstructorClient(Protocol):
    chat: _InstructorChat


def _get_instructor_client() -> instructor.Instructor:
    global _openai_client, _instructor_client
    raise RuntimeError("_get_instructor_client 已弃用，请使用 _get_instructor_client_async")


async def _get_instructor_client_async() -> instructor.Instructor:
    if _instructor_client is not None:
        return _instructor_client
    await initialize_openai_client()
    if _instructor_client is None:
        raise RuntimeError("OpenAI client initialization failed")
    return _instructor_client


async def initialize_openai_client(policy: AIProviderPolicy | None = None) -> None:
    global _openai_client, _instructor_client
    policy = policy or AIProviderPolicy.from_config()
    async with _openai_client_lock:
        if _instructor_client is None:
            if _openai_client is None:
                _openai_client = AsyncOpenAI(
                    api_key=policy.api_key,
                    base_url=policy.api_url,
                    http_client=get_http_client(),
                    timeout=httpx.Timeout(60.0, connect=10.0),
                )
            _instructor_client = instructor.from_openai(_openai_client, mode=instructor.Mode.JSON)


async def _create_with_completion(
    client: instructor.Instructor, *, model: str, user_prompt: str, policy: AIProviderPolicy | None = None
) -> tuple[WebhookAnalysisResult, _Completion]:
    policy = policy or AIProviderPolicy.from_config()
    typed = cast(_InstructorClient, client)
    return await typed.chat.completions.create_with_completion(
        model=model,
        response_model=WebhookAnalysisResult,
        messages=[
            {"role": "system", "content": policy.system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=policy.temperature,
        max_retries=2,
    )


async def reset_openai_client() -> None:
    global _openai_client, _instructor_client
    async with _openai_client_lock:
        _openai_client = _instructor_client = None


async def _analyze_with_openai_tracked(
    data: dict[str, Any], source: str, *, policy: AIProviderPolicy | None = None
) -> tuple[AnalysisResult, int, int]:
    policy = policy or AIProviderPolicy.from_config()
    client = await _get_instructor_client_async()
    cleaned_data = await sanitize_for_ai_async(data)
    data_yaml = yaml.dump(cleaned_data, allow_unicode=True, default_flow_style=False, sort_keys=False)
    user_prompt = (await load_user_prompt_template()).format(source=source, data_json=data_yaml)

    with otel_span("ai.openai_call", {"source": source, "model": policy.model}) as s:
        res, completion = await _create_with_completion(
            client, model=policy.model, user_prompt=user_prompt, policy=policy
        )

        t_in = completion.usage.prompt_tokens if completion.usage else 0
        t_out = completion.usage.completion_tokens if completion.usage else 0
        cost = policy.cost_for_tokens(t_in, t_out)
        AI_TOKENS_TOTAL.labels(policy.model, "input").inc(t_in)
        AI_TOKENS_TOTAL.labels(policy.model, "output").inc(t_out)
        AI_COST_USD_TOTAL.labels(model=policy.model).inc(cost)
        if s:
            s.set_attribute("tokens_in", t_in)
            s.set_attribute("tokens_out", t_out)
    return res.to_dict(), t_in, t_out


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential_jitter(initial=2, max=30, jitter=2),
    reraise=True,
    retry=retry_if_exception(
        lambda e: isinstance(e, (httpx.RequestError, httpx.TimeoutException, ConnectionError, TimeoutError))
    ),
    before_sleep=before_sleep_log(logger, logging.WARNING),
)
async def _call_ai_with_retry(parsed_data: dict[str, Any], source: str) -> tuple[dict[str, Any], int, int]:
    start = time.time()
    try:
        res, t_in, t_out = await _analyze_with_openai_tracked(parsed_data, source)
        AI_ANALYSIS_DURATION_SECONDS.labels(source=sanitize_source(source), engine="openai").observe(
            time.time() - start
        )
        return res, t_in, t_out
    except Exception as e:
        OPENAI_ERRORS_TOTAL.labels(type=type(e).__name__.lower()).inc()
        raise


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


async def _send_ai_error_alert(webhook_data: WebhookData, error_reason: str, is_degraded: bool = False) -> None:
    from services.operations.ai_error_notifications import send_ai_error_alert

    await send_ai_error_alert(webhook_data, error_reason, is_degraded=is_degraded)


# ── 解析与规则分析 ─────────────────────────────────────────────────────────────


def analyze_with_rules(
    data: dict[str, Any], source: str, *, policy: RuleAnalysisPolicy | None = None
) -> AnalysisResult:
    policy = policy or RuleAnalysisPolicy.from_config()
    start_time = time.time()
    res = {
        "source": source,
        "event_type": "unknown",
        "importance": "medium",
        "summary": "规则分析（AI 降级）",
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
        ALERT_NUMERIC_PARSE_FAILURE_TOTAL.labels(
            source=sanitize_source(source),
            field=field,
            reason=reason,
        ).inc()
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

    AI_ANALYSIS_DURATION_SECONDS.labels(source=sanitize_source(source), engine="rule").observe(time.time() - start_time)
    return res


# ── 主业务逻辑 ─────────────────────────────────────────────────────────────


async def analyze_webhook_with_ai(
    webhook_data: WebhookData, alert_hash: str | None = None, skip_cache: bool = False
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
        else:
            # 即使不降级，也要发送错误通知
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

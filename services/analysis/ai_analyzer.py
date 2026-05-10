"""AI 分析核心引擎

集成 Prompt 管理、缓存、LLM 调用、结构化解析与成本追踪。
"""

import asyncio
import logging
import math
import os
import time
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, cast

import httpx
import instructor
import orjson
import yaml
from openai import AsyncOpenAI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from core.circuit_breaker import CircuitBreakerOpenException, feishu_cb
from core.config import Config
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
from db.session import count_with_timeout, session_scope
from models import AIUsageLog, DeepAnalysis, WebhookEvent
from schemas import WebhookAnalysisResult
from services.webhooks.payload_sanitizer import sanitize_for_ai_async
from services.webhooks.types import AnalysisResult, WebhookData

_last_policy_refresh_at: float = 0.0
_policy_refresh_lock = asyncio.Lock()
_prompt_template_lock = asyncio.Lock()
_openai_client_lock = asyncio.Lock()


def _get_config_source(key: str) -> str:
    meta = Config.get_meta(key)
    source = meta.get("source")
    if source:
        return str(source)
    return "env" if os.getenv(key) is not None else "default"


def _format_meta_time(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    return str(value)


async def _maybe_refresh_runtime_policies(keys: tuple[str, ...], min_interval_seconds: int = 30) -> None:
    global _last_policy_refresh_at
    if not Config.server.ENABLE_RUNTIME_CONFIG:
        return
    async with _policy_refresh_lock:
        now = time.time()
        if now - _last_policy_refresh_at < min_interval_seconds:
            return
        before_url = Config.ai.OPENAI_API_URL
        before_key = Config.ai.OPENAI_API_KEY
        await Config.load_from_db()
        if before_url != Config.ai.OPENAI_API_URL or before_key != Config.ai.OPENAI_API_KEY:
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


async def load_user_prompt_template() -> str:
    global _user_prompt_template, _user_prompt_source
    async with _prompt_template_lock:
        if _user_prompt_template is not None:
            return _user_prompt_template

        if Config.ai.AI_USER_PROMPT:
            _user_prompt_source, _user_prompt_template = "env:AI_USER_PROMPT", Config.ai.AI_USER_PROMPT
            return _user_prompt_template

        prompt_file = Config.ai.AI_USER_PROMPT_FILE
        if prompt_file:
            file_path = _resolve_prompt_path(prompt_file)
            if file_path.exists():
                try:
                    with open(file_path, encoding="utf-8") as f:
                        _user_prompt_template = f.read()
                    _user_prompt_source = f"file:{file_path}"
                    return _user_prompt_template
                except Exception as e:
                    logger.warning(f"从文件加载 prompt 模板失败: {e}")

        _user_prompt_source = "builtin:default"
        _user_prompt_template = """请分析以下 webhook 事件：
**来源**: {source}
**数据内容**:
```yaml
{data_json}
```
请识别事件的类型、严重程度，并提供摘要、影响评估和处理建议。"""
        return _user_prompt_template


async def reload_user_prompt_template() -> str:
    global _user_prompt_template
    async with _prompt_template_lock:
        _user_prompt_template = None
    return await load_user_prompt_template()


# ── 缓存管理 ─────────────────────────────────────────────────────────────


def get_cache_key(alert_hash: str) -> str:
    return f"analysis_{alert_hash}"


async def get_cached_analysis(alert_hash: str) -> AnalysisResult | None:
    if not Config.ai.CACHE_ENABLED:
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
        hits = await redis_incr_with_expire(counter_key, Config.ai.ANALYSIS_CACHE_TTL)
        res.update({"_cache_hit": True, "_cache_hit_count": hits})
        return res
    except Exception as e:
        logger.warning(f"读取缓存失败: {e}")
        return None


async def save_to_cache(alert_hash: str, analysis_result: AnalysisResult) -> bool:
    if not Config.ai.CACHE_ENABLED:
        return False
    try:
        from core.redis_client import redis_setex_bytes, redis_setex_str

        ck = get_cache_key(alert_hash)
        res_to_cache = {k: v for k, v in analysis_result.items() if not k.startswith("_")}
        cached_bytes = orjson.dumps(res_to_cache)
        counter_key = f"{ck}:hits"
        await redis_setex_bytes(ck, Config.ai.ANALYSIS_CACHE_TTL, cached_bytes)
        await redis_setex_str(counter_key, Config.ai.ANALYSIS_CACHE_TTL, "0")
        return True
    except Exception as e:
        logger.warning(f"保存缓存失败: {e}")
        return False


async def log_ai_usage(
    route_type: str,
    alert_hash: str,
    source: str,
    model: str | None = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cache_hit: bool = False,
) -> None:
    try:
        cost = 0.0
        if route_type == "ai" and tokens_in > 0:
            cost = (tokens_in / 1000) * Config.ai.AI_COST_PER_1K_INPUT_TOKENS + (
                tokens_out / 1000
            ) * Config.ai.AI_COST_PER_1K_OUTPUT_TOKENS
        async with session_scope() as session:
            session.add(
                AIUsageLog(
                    model=model or Config.ai.OPENAI_MODEL,
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
        logger.warning(f"记录 AI 使用日志失败: {e}")


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


async def initialize_openai_client() -> None:
    global _openai_client, _instructor_client
    async with _openai_client_lock:
        if _instructor_client is None:
            if _openai_client is None:
                _openai_client = AsyncOpenAI(
                    api_key=Config.ai.OPENAI_API_KEY,
                    base_url=Config.ai.OPENAI_API_URL,
                    http_client=get_http_client(),
                    timeout=httpx.Timeout(60.0, connect=10.0),
                )
            _instructor_client = instructor.from_openai(_openai_client, mode=instructor.Mode.JSON)


async def _create_with_completion(
    client: instructor.Instructor, *, model: str, user_prompt: str
) -> tuple[WebhookAnalysisResult, _Completion]:
    typed = cast(_InstructorClient, client)
    return await typed.chat.completions.create_with_completion(
        model=model,
        response_model=WebhookAnalysisResult,
        messages=[
            {"role": "system", "content": Config.ai.AI_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=Config.ai.OPENAI_TEMPERATURE,
        max_retries=2,
    )


async def reset_openai_client() -> None:
    global _openai_client, _instructor_client
    async with _openai_client_lock:
        _openai_client = _instructor_client = None


async def _analyze_with_openai_tracked(data: dict[str, Any], source: str) -> tuple[AnalysisResult, int, int]:
    client = await _get_instructor_client_async()
    cleaned_data = await sanitize_for_ai_async(data)
    data_yaml = yaml.dump(cleaned_data, allow_unicode=True, default_flow_style=False, sort_keys=False)
    user_prompt = (await load_user_prompt_template()).format(source=source, data_json=data_yaml)

    with otel_span("ai.openai_call", {"source": source, "model": Config.ai.OPENAI_MODEL}) as s:
        res, completion = await _create_with_completion(client, model=Config.ai.OPENAI_MODEL, user_prompt=user_prompt)

        t_in = completion.usage.prompt_tokens if completion.usage else 0
        t_out = completion.usage.completion_tokens if completion.usage else 0
        cost = (t_in / 1000) * Config.ai.AI_COST_PER_1K_INPUT_TOKENS + (
            t_out / 1000
        ) * Config.ai.AI_COST_PER_1K_OUTPUT_TOKENS
        AI_TOKENS_TOTAL.labels(Config.ai.OPENAI_MODEL, "input").inc(t_in)
        AI_TOKENS_TOTAL.labels(Config.ai.OPENAI_MODEL, "output").inc(t_out)
        AI_COST_USD_TOTAL.labels(model=Config.ai.OPENAI_MODEL).inc(cost)
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


async def _send_ai_error_alert(webhook_data: WebhookData, error_reason: str, is_degraded: bool = False) -> None:
    try:
        if not Config.ai.ENABLE_FORWARD or not Config.ai.FORWARD_URL:
            return
        import hashlib

        from core.redis_client import redis_set_nx_ex

        # 根据错误内容生成短 hash，不同类型的错误不会互相阻塞，但同一种错误 1 小时内只报一次
        error_hash = hashlib.md5(error_reason[:100].encode("utf-8"), usedforsecurity=False).hexdigest()[:8]
        lock_key = f"ai_error_alert_lock:{error_hash}"

        # 冷却时间：3600 秒 (1 小时)
        if not await redis_set_nx_ex(lock_key, "1", 3600):
            return

        title = "⚠️ AI 分析降级通知" if is_degraded else "❌ AI 分析失败通知"
        template = "orange" if is_degraded else "red"

        card = {
            "msg_type": "interactive",
            "card": {
                "header": {"title": {"tag": "plain_text", "content": title}, "template": template},
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"**来源**: {webhook_data.get('source', 'uk')}\n**原因**: {error_reason}",
                        },
                    }
                ],
            },
        }
        await feishu_cb.call_async(get_http_client().post, Config.ai.FORWARD_URL, json=card, timeout=10)
    except CircuitBreakerOpenException as e:
        logger.warning("发送 AI 错误通知被熔断器拦截: %s", e)
    except Exception as e:
        logger.error(f"发送 AI 错误通知失败: {e}")


# ── 解析与规则分析 ─────────────────────────────────────────────────────────────


def analyze_with_rules(data: dict[str, Any], source: str) -> AnalysisResult:
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

    def _split_keywords(v: str) -> list[str]:
        return [p.strip().lower() for p in str(v).split(",") if p.strip()]

    high_kw = _split_keywords(Config.ai.RULE_HIGH_KEYWORDS)
    warn_kw = _split_keywords(Config.ai.RULE_WARN_KEYWORDS)
    metric_kw = _split_keywords(Config.ai.RULE_METRIC_KEYWORDS)

    importance = "medium"
    if level in high_kw or any(k in level for k in high_kw) or any(k in name_l for k in high_kw):
        importance = "high"
    elif level in warn_kw or any(k in level for k in warn_kw) or any(k in name_l for k in warn_kw):
        importance = "medium"
    elif any(k in level for k in ("info", "information", "notice", "ok", "resolved", "success", "normal", "恢复")):
        importance = "low"

    cur_val = data.get("CurrentValue") or data.get("current_value") or data.get("current") or data.get("value")
    thr_val = data.get("Threshold") or data.get("threshold") or data.get("limit")
    multiplier = float(Config.ai.RULE_THRESHOLD_MULTIPLIER or 4.0)

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
    source, parsed = webhook_data.get("source", "unknown"), webhook_data.get("parsed_data", {})
    if not alert_hash:
        alert_hash = WebhookEvent.generate_hash(parsed, source)

    if Config.ai.CACHE_ENABLED and not skip_cache:
        cached = await get_cached_analysis(alert_hash)
        if cached:
            hits = cached.get("_cache_hit_count", 1)
            logger.info("[AI] Redis 缓存命中 source=%s hits=%s hash=%s...", source, hits, alert_hash[:12])
            await log_ai_usage("cache", alert_hash, source, cache_hit=True)
            return {**cached, "_route_type": "cache"}

    if not Config.ai.ENABLE_AI_ANALYSIS or not Config.ai.OPENAI_API_KEY:
        reason = "disabled" if not Config.ai.ENABLE_AI_ANALYSIS else "no_api_key"
        logger.info("[AI] 降级为规则分析 source=%s reason=%s", source, reason)
        res = analyze_with_rules(parsed, source)
        res.update({"_degraded": True, "_route_type": "rule"})
        await log_ai_usage("rule", alert_hash, source)

        # 触发降级通知
        if Config.ai.ENABLE_AI_DEGRADATION and reason == "no_api_key":
            await _send_ai_error_alert(
                webhook_data,
                "配置了开启 AI 分析，但当前 Worker 进程未能读取到 OPENAI_API_KEY，已自动降级为规则分析。",
                is_degraded=True,
            )

        return res

    try:
        model_meta = Config.get_meta("OPENAI_MODEL")
        logger.debug(
            "[AI] 发起 OpenAI 请求 source=%s model=%s model_source=%s model_updated_at=%s hash=%s...",
            source,
            Config.ai.OPENAI_MODEL,
            _get_config_source("OPENAI_MODEL"),
            _format_meta_time(model_meta.get("updated_at") if isinstance(model_meta, dict) else None),
            alert_hash[:12],
        )
        analysis, t_in, t_out = await _call_ai_with_retry(parsed, source)
        logger.info(
            "[AI] 分析完成 source=%s model=%s tokens_in=%d tokens_out=%d importance=%s",
            source,
            Config.ai.OPENAI_MODEL,
            t_in,
            t_out,
            str(analysis.get("importance", "unknown")).lower().rsplit(".", 1)[-1],
        )
        if not analysis.get("_degraded"):
            await save_to_cache(alert_hash, analysis)
        await log_ai_usage("ai", alert_hash, source, model=Config.ai.OPENAI_MODEL, tokens_in=t_in, tokens_out=t_out)
        return {**analysis, "_route_type": "ai"}
    except Exception as e:
        logger.error("[AI] 分析失败 source=%s error=%s", source, e)
        if Config.ai.ENABLE_AI_DEGRADATION:
            logger.info("[AI] 降级为规则分析 source=%s reason=ai_error", source)
            res = analyze_with_rules(parsed, source)
            res.update({"_degraded": True, "_route_type": "rule"})
            await _send_ai_error_alert(webhook_data, str(e), is_degraded=True)
            return res
        else:
            # 即使不降级，也要发送错误通知
            await _send_ai_error_alert(webhook_data, str(e), is_degraded=False)
            raise


# ── 统计与列表查询 ─────────────────────────────────────────────────────────────


async def get_ai_usage_stats(session: AsyncSession, period: str = "day") -> dict[str, Any]:
    now = datetime.now()
    if period == "day":
        delta = timedelta(days=1)
    elif period == "week":
        delta = timedelta(days=7)
    elif period == "month":
        delta = timedelta(days=30)
    else:
        delta = timedelta(days=365)

    start_time = now - delta

    total_stmt = select(func.count(AIUsageLog.id)).filter(AIUsageLog.timestamp >= start_time)
    total = await count_with_timeout(session, total_stmt) or 0

    route_stmt = (
        select(AIUsageLog.route_type, func.count(AIUsageLog.id))
        .filter(AIUsageLog.timestamp >= start_time)
        .group_by(AIUsageLog.route_type)
    )
    route_stats = (await session.execute(route_stmt)).all()
    route_breakdown = {r[0]: r[1] for r in route_stats}

    stats_stmt = select(
        func.sum(AIUsageLog.tokens_in), func.sum(AIUsageLog.tokens_out), func.sum(AIUsageLog.cost_estimate)
    ).filter(AIUsageLog.timestamp >= start_time)
    stats = (await session.execute(stats_stmt)).first()
    tokens_in = int(stats[0] or 0) if stats is not None else 0
    tokens_out = int(stats[1] or 0) if stats is not None else 0
    total_cost = float(stats[2] or 0.0) if stats is not None else 0.0

    # 查询缓存条目数（曾产生 AI 调用的唯一 alert_hash 数）
    cache_entries_stmt = select(func.count(func.distinct(AIUsageLog.alert_hash))).filter(
        AIUsageLog.timestamp >= start_time,
        AIUsageLog.route_type == "ai",
        AIUsageLog.alert_hash.isnot(None),
    )
    cache_entries = (await session.execute(cache_entries_stmt)).scalar() or 0

    cache_hits = route_breakdown.get("cache", 0)
    reuse_hits = route_breakdown.get("reuse", 0)
    total_hits = cache_hits + reuse_hits
    avg_hits = round(reuse_hits / cache_entries, 2) if cache_entries > 0 else 0.0
    hit_rate = round(total_hits / max(total, 1) * 100, 2)

    ai_calls = route_breakdown.get("ai", 0)
    avg_cost_per_ai_call = total_cost / ai_calls if ai_calls > 0 else 0.0
    saved_estimate = round(total_hits * avg_cost_per_ai_call, 6)

    return {
        "total_calls": total,
        "route_breakdown": route_breakdown,
        "percentages": {k: round(v / max(total, 1) * 100, 2) for k, v in route_breakdown.items()},
        "tokens": {"input": tokens_in, "output": tokens_out, "total": tokens_in + tokens_out},
        "cost": {"total": total_cost, "saved_estimate": saved_estimate},
        "cache_statistics": {
            "total_cache_entries": cache_entries,
            "total_hits": total_hits,
            "avg_hits_per_entry": avg_hits,
            "cache_hit_rate": hit_rate,
            "saved_calls": total_hits,
        },
        "trend": [],
    }


async def get_deep_analysis_list(
    session: AsyncSession,
    page: int = 1,
    per_page: int = 20,
    cursor: int | None = None,
    status_filter: str = "",
    engine_filter: str = "",
    max_page: int = 500,
) -> dict[str, Any]:
    from sqlalchemy import func

    # 基础过滤条件
    filters = []
    if cursor:
        filters.append(DeepAnalysis.id < cursor)
    if status_filter:
        filters.append(DeepAnalysis.status == status_filter)
    if engine_filter:
        filters.append(DeepAnalysis.engine == engine_filter)

    # COUNT 查询
    count_query = select(func.count()).select_from(DeepAnalysis)
    if status_filter:
        count_query = count_query.where(DeepAnalysis.status == status_filter)
    if engine_filter:
        count_query = count_query.where(DeepAnalysis.engine == engine_filter)
    total = (await session.execute(count_query)).scalar() or 0
    total_pages = max(1, (total + per_page - 1) // per_page)

    # 数据查询（游标或页码 offset）
    query = (
        select(DeepAnalysis, WebhookEvent)
        .outerjoin(WebhookEvent, WebhookEvent.id == DeepAnalysis.webhook_event_id)
        .order_by(DeepAnalysis.id.desc())
    )
    for f in filters:
        query = query.where(f)
    if not cursor:
        query = query.offset((page - 1) * per_page)
    query = query.limit(per_page)

    res = await session.execute(query)
    rows = res.all()
    items = []
    for rec, evt in rows:
        d = rec.to_dict()
        d["source"] = evt.source if evt else None
        d["is_duplicate"] = bool(evt.is_duplicate) if evt else False
        d["beyond_window"] = bool(evt.beyond_window) if evt else False
        items.append(d)
    next_cursor = items[-1]["id"] if items else None
    return {
        "items": items,
        "per_page": per_page,
        "page": page,
        "total": total,
        "total_pages": total_pages,
        "next_cursor": next_cursor,
    }


async def get_deep_analyses_for_webhook(session: AsyncSession, webhook_id: int) -> list[DeepAnalysis]:
    stmt = select(DeepAnalysis).filter_by(webhook_event_id=webhook_id).order_by(DeepAnalysis.created_at.desc())
    res = await session.execute(stmt)
    return list(res.scalars().all())


async def _send_openclaw_failure_notification(webhook_data: WebhookData, source: str, error: str) -> None:
    try:
        from adapters.ecosystem_adapters import send_feishu_deep_analysis

        if not Config.ai.DEEP_ANALYSIS_FEISHU_WEBHOOK:
            return
        await send_feishu_deep_analysis(
            Config.ai.DEEP_ANALYSIS_FEISHU_WEBHOOK,
            {
                "analysis_result": {"root_cause": error, "impact": "分析失败，无法评估影响范围"},
                "engine": "openclaw",
                "duration_seconds": 0,
            },
            source,
            int(webhook_data.get("id", 0) or 0),
        )
    except Exception as e:
        logger.error(f"发送失败通知失败: {e}")

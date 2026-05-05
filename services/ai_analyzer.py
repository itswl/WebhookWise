"""AI 分析核心引擎

集成 Prompt 管理、缓存、LLM 调用、结构化解析与成本追踪。
"""

import contextlib
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

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

from core.circuit_breaker import feishu_cb
from core.config import Config, policies
from core.http_client import get_http_client
from core.logger import logger
from core.metrics import (
    AI_ANALYSIS_DURATION_SECONDS,
    AI_COST_USD_TOTAL,
    AI_TOKENS_TOTAL,
    OPENAI_ERRORS_TOTAL,
    sanitize_source,
)
from db.session import count_with_timeout, session_scope
from models import AIUsageLog, DeepAnalysis, WebhookEvent
from schemas import WebhookAnalysisResult
from services.payload_sanitizer import sanitize_for_ai_async

# 类型别名
WebhookData = dict[str, Any]
AnalysisResult = dict[str, Any]


# ── Prompt 管理 ─────────────────────────────────────────────────────────────

_user_prompt_template: str | None = None
_user_prompt_source: str = "unknown"


def get_prompt_source() -> str:
    return _user_prompt_source


def load_user_prompt_template() -> str:
    global _user_prompt_template, _user_prompt_source
    if _user_prompt_template is not None:
        return _user_prompt_template

    if Config.ai.AI_USER_PROMPT:
        _user_prompt_source, _user_prompt_template = "env:AI_USER_PROMPT", Config.ai.AI_USER_PROMPT
        return _user_prompt_template

    prompt_file = Config.ai.AI_USER_PROMPT_FILE
    if prompt_file:
        file_path = Path(prompt_file)
        if not file_path.is_absolute():
            file_path = Path(__file__).parent.parent / file_path
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


def reload_user_prompt_template() -> str:
    global _user_prompt_template
    _user_prompt_template = None
    return load_user_prompt_template()


# ── 缓存管理 ─────────────────────────────────────────────────────────────


def get_cache_key(alert_hash: str) -> str:
    return f"analysis_{alert_hash}"


async def get_cached_analysis(alert_hash: str) -> dict | None:
    if not Config.ai.CACHE_ENABLED:
        return None
    try:
        from core.redis_client import get_redis
        r, ck = get_redis(), get_cache_key(alert_hash)
        cached_json = await r.get(ck)
        if not cached_json:
            return None
        res = orjson.loads(cached_json)
        counter_key = f"{ck}:hits"
        pipe = r.pipeline()
        pipe.incr(counter_key)
        pipe.expire(counter_key, Config.ai.ANALYSIS_CACHE_TTL)
        hits = (await pipe.execute())[0]
        res.update({"_cache_hit": True, "_cache_hit_count": hits})
        return res
    except Exception as e:
        logger.warning(f"读取缓存失败: {e}")
        return None


async def save_to_cache(alert_hash: str, analysis_result: dict) -> bool:
    if not Config.ai.CACHE_ENABLED:
        return False
    try:
        from core.redis_client import get_redis
        r, ck = get_redis(), get_cache_key(alert_hash)
        res_to_cache = {k: v for k, v in analysis_result.items() if not k.startswith("_")}
        cached_bytes = orjson.dumps(res_to_cache)
        counter_key = f"{ck}:hits"
        pipe = r.pipeline()
        pipe.setex(ck, Config.ai.ANALYSIS_CACHE_TTL, cached_bytes)
        pipe.setex(counter_key, Config.ai.ANALYSIS_CACHE_TTL, "0")
        await pipe.execute()
        with contextlib.suppress(Exception):
            await r.publish(f"analysis_done:{alert_hash}", "1")
        return True
    except Exception as e:
        logger.warning(f"保存缓存失败: {e}")
        return False


async def log_ai_usage(
    route_type: str, alert_hash: str, source: str, model: str | None = None,
    tokens_in: int = 0, tokens_out: int = 0, cache_hit: bool = False
) -> None:
    try:
        cost = 0.0
        if route_type == "ai" and tokens_in > 0:
            cost = (tokens_in / 1000) * Config.ai.AI_COST_PER_1K_INPUT_TOKENS + (
                tokens_out / 1000
            ) * Config.ai.AI_COST_PER_1K_OUTPUT_TOKENS
        async with session_scope() as session:
            session.add(AIUsageLog(
                model=model or policies.ai.OPENAI_MODEL, tokens_in=tokens_in, tokens_out=tokens_out,
                cost_estimate=cost, cache_hit=cache_hit, route_type=route_type,
                alert_hash=alert_hash, source=source
            ))
    except Exception as e:
        logger.warning(f"记录 AI 使用日志失败: {e}")


# ── LLM 调用 ─────────────────────────────────────────────────────────────

_openai_client: AsyncOpenAI | None = None
_instructor_client: instructor.Instructor | None = None


def _get_instructor_client() -> instructor.Instructor:
    global _openai_client, _instructor_client
    if _instructor_client is None:
        if _openai_client is None:
            _openai_client = AsyncOpenAI(
                api_key=policies.ai.OPENAI_API_KEY, base_url=policies.ai.OPENAI_API_URL,
                http_client=get_http_client(), timeout=httpx.Timeout(60.0, connect=10.0)
            )
        _instructor_client = instructor.from_openai(_openai_client, mode=instructor.Mode.JSON)
    return _instructor_client


def reset_openai_client():
    global _openai_client, _instructor_client
    _openai_client = _instructor_client = None


async def _analyze_with_openai_tracked(data: dict[str, Any], source: str) -> tuple[AnalysisResult, int, int]:
    client = _get_instructor_client()
    cleaned_data = await sanitize_for_ai_async(data)
    data_yaml = yaml.dump(cleaned_data, allow_unicode=True, default_flow_style=False, sort_keys=False)
    user_prompt = load_user_prompt_template().format(source=source, data_json=data_yaml)

    res, completion = await client.chat.completions.create_with_completion(
        model=policies.ai.OPENAI_MODEL, response_model=WebhookAnalysisResult,
        messages=[{"role": "system", "content": policies.ai.AI_SYSTEM_PROMPT}, {"role": "user", "content": user_prompt}],
        temperature=Config.ai.OPENAI_TEMPERATURE, max_retries=2
    )

    t_in = completion.usage.prompt_tokens if completion.usage else 0
    t_out = completion.usage.completion_tokens if completion.usage else 0
    cost = (t_in / 1000) * Config.ai.AI_COST_PER_1K_INPUT_TOKENS + (t_out / 1000) * Config.ai.AI_COST_PER_1K_OUTPUT_TOKENS
    AI_TOKENS_TOTAL.labels(model=policies.ai.OPENAI_MODEL, token_type="input").inc(t_in)
    AI_TOKENS_TOTAL.labels(model=policies.ai.OPENAI_MODEL, token_type="output").inc(t_out)
    AI_COST_USD_TOTAL.labels(model=policies.ai.OPENAI_MODEL).inc(cost)
    return res.to_dict(), t_in, t_out


@retry(
    stop=stop_after_attempt(3), wait=wait_exponential_jitter(initial=2, max=30, jitter=2), reraise=True,
    retry=retry_if_exception(lambda e: isinstance(e, (httpx.RequestError, httpx.TimeoutException, ConnectionError, TimeoutError))),
    before_sleep=before_sleep_log(logger, logging.WARNING),
)
async def _call_ai_with_retry(parsed_data: dict[str, Any], source: str) -> tuple[dict[str, Any], int, int]:
    start = time.time()
    try:
        res, t_in, t_out = await _analyze_with_openai_tracked(parsed_data, source)
        AI_ANALYSIS_DURATION_SECONDS.labels(source=sanitize_source(source), engine="openai").observe(time.time() - start)
        return res, t_in, t_out
    except Exception as e:
        OPENAI_ERRORS_TOTAL.labels(type=type(e).__name__.lower()).inc()
        raise


async def _send_degradation_alert(webhook_data: WebhookData, error_reason: str) -> None:
    try:
        if not policies.ai.ENABLE_FORWARD or not policies.ai.FORWARD_URL:
            return
        from core.redis_client import get_redis
        if not await get_redis().set("ai_degradation_alert_lock", "1", nx=True, ex=86400):
            return

        card = {
            "msg_type": "interactive",
            "card": {
                "header": {"title": {"tag": "plain_text", "content": "⚠️ AI 分析降级通知"}, "template": "orange"},
                "elements": [
                    {"tag": "div", "text": {"tag": "lark_md", "content": f"**来源**: {webhook_data.get('source', 'uk')}\n**原因**: {error_reason}"}}
                ]
            }
        }
        await feishu_cb.call_async(get_http_client().post, policies.ai.FORWARD_URL, json=card, timeout=10)
    except Exception as e:
        logger.error(f"发送降级通知失败: {e}")


# ── 解析与规则分析 ─────────────────────────────────────────────────────────────


def analyze_with_rules(data: dict[str, Any], source: str) -> AnalysisResult:
    start_time = time.time()
    res = {
        "source": source, "event_type": "unknown", "importance": "medium",
        "summary": "规则分析（AI 降级）", "actions": ["查看告警详情"], "risks": ["分析可能不准"]
    }

    # 极简规则判断
    rule_name = data.get("RuleName") or data.get("alert_name") or "unknown"
    res["event_type"] = rule_name
    level = str(data.get("Level", "")).lower()
    high_kw = policies.ai.RULE_HIGH_KEYWORDS.lower().split(",")
    if level in high_kw or any(k in rule_name.lower() for k in high_kw):
        res["importance"], res["summary"] = "high", f"🔴 严重告警: {rule_name}"

    AI_ANALYSIS_DURATION_SECONDS.labels(source=sanitize_source(source), engine="rule").observe(time.time() - start_time)
    return res


# ── 主业务逻辑 ─────────────────────────────────────────────────────────────


async def analyze_webhook_with_ai(
    webhook_data: WebhookData, alert_hash: str | None = None, skip_cache: bool = False
) -> AnalysisResult:
    source, parsed = webhook_data.get("source", "unknown"), webhook_data.get("parsed_data", {})
    if not alert_hash:
        alert_hash = WebhookEvent.generate_hash(parsed, source)

    if Config.ai.CACHE_ENABLED and not skip_cache:
        cached = await get_cached_analysis(alert_hash)
        if cached:
            await log_ai_usage("cache", alert_hash, source, cache_hit=True)
            return {**cached, "_route_type": "cache"}

    if not policies.ai.ENABLE_AI_ANALYSIS or not policies.ai.OPENAI_API_KEY:
        res = analyze_with_rules(parsed, source)
        res.update({"_degraded": True, "_route_type": "rule"})
        await log_ai_usage("rule", alert_hash, source)
        return res

    try:
        analysis, t_in, t_out = await _call_ai_with_retry(parsed, source)
        await save_to_cache(alert_hash, analysis)
        await log_ai_usage("ai", alert_hash, source, model=policies.ai.OPENAI_MODEL, tokens_in=t_in, tokens_out=t_out)
        return {**analysis, "_route_type": "ai"}
    except Exception as e:
        logger.error(f"AI 分析失败: {e}")
        if Config.ai.ENABLE_AI_DEGRADATION:
            res = analyze_with_rules(parsed, source)
            res.update({"_degraded": True, "_route_type": "rule"})
            await _send_degradation_alert(webhook_data, str(e))
            return res
        raise


# ── 统计与列表查询 ─────────────────────────────────────────────────────────────


async def get_ai_usage_stats(session: AsyncSession, period: str = "day"):
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

    route_stmt = select(AIUsageLog.route_type, func.count(AIUsageLog.id)).filter(
        AIUsageLog.timestamp >= start_time
    ).group_by(AIUsageLog.route_type)
    route_stats = (await session.execute(route_stmt)).all()
    route_breakdown = {r[0]: r[1] for r in route_stats}

    stats_stmt = select(
        func.sum(AIUsageLog.tokens_in), func.sum(AIUsageLog.tokens_out), func.sum(AIUsageLog.cost_estimate)
    ).filter(AIUsageLog.timestamp >= start_time)
    stats = (await session.execute(stats_stmt)).first()

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
    total_cost = float(stats[2] or 0.0)
    avg_cost_per_ai_call = total_cost / ai_calls if ai_calls > 0 else 0.0
    saved_estimate = round(total_hits * avg_cost_per_ai_call, 6)

    return {
        "total_calls": total, "route_breakdown": route_breakdown,
        "percentages": {k: round(v / max(total, 1) * 100, 2) for k, v in route_breakdown.items()},
        "tokens": {"input": stats[0] or 0, "output": stats[1] or 0, "total": (stats[0] or 0) + (stats[1] or 0)},
        "cost": {"total": total_cost, "saved_estimate": saved_estimate},
        "cache_statistics": {
            "total_cache_entries": cache_entries,
            "total_hits": total_hits,
            "avg_hits_per_entry": avg_hits,
            "cache_hit_rate": hit_rate,
            "saved_calls": total_hits,
        },
        "trend": []
    }


async def get_deep_analysis_list(
    session: AsyncSession, page: int = 1, per_page: int = 20, cursor: int | None = None,
    status_filter: str = "", engine_filter: str = "", max_page: int = 500
):
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
    query = select(DeepAnalysis, WebhookEvent).outerjoin(
        WebhookEvent, WebhookEvent.id == DeepAnalysis.webhook_event_id
    ).order_by(DeepAnalysis.id.desc())
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
    return {"items": items, "per_page": per_page, "page": page, "total": total, "total_pages": total_pages, "next_cursor": next_cursor}


async def get_deep_analyses_for_webhook(session: AsyncSession, webhook_id: int):
    stmt = select(DeepAnalysis).filter_by(webhook_event_id=webhook_id).order_by(DeepAnalysis.created_at.desc())
    res = await session.execute(stmt)
    return res.scalars().all()


async def _send_openclaw_failure_notification(webhook_data: WebhookData, source: str, error: str) -> None:
    try:
        from adapters.ecosystem_adapters import send_feishu_deep_analysis
        if not Config.ai.DEEP_ANALYSIS_FEISHU_WEBHOOK:
            return
        await send_feishu_deep_analysis(
            Config.ai.DEEP_ANALYSIS_FEISHU_WEBHOOK,
            {"analysis_result": {"root_cause": error, "impact": "分析失败，无法评估影响范围"}, "engine": "openclaw", "duration_seconds": 0},
            source, webhook_data.get("id", 0)
        )
    except Exception as e:
        logger.error(f"发送失败通知失败: {e}")

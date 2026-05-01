from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import Config
from core.redis_client import get_redis
from crud.helpers import count_with_timeout
from models import AIUsageLog


async def get_ai_usage_stats(session: AsyncSession, period: str = "day"):
    now = datetime.now()
    start_time = now
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

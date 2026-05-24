"""Read-side queries for AI usage and deep-analysis records."""

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.session import count_with_timeout
from core.datetime_utils import utcnow
from models import AIUsageLog, DeepAnalysis, WebhookEvent
from schemas import deep_analysis_to_dict


async def get_ai_usage_stats(session: AsyncSession, period: str = "day") -> dict[str, Any]:
    now = utcnow()
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

    cache_entries_stmt = select(func.count(func.distinct(AIUsageLog.alert_hash))).filter(
        AIUsageLog.timestamp >= start_time,
        AIUsageLog.route_type == "ai",
        AIUsageLog.alert_hash.isnot(None),
    )
    cache_entries = (await session.execute(cache_entries_stmt)).scalar() or 0

    # 汇总所有复用/缓存命中（redis_reuse, db_reuse, cache, reuse）
    reuses = route_breakdown.get("redis_reuse", 0) + route_breakdown.get("db_reuse", 0)
    cache_hits = route_breakdown.get("cache", 0)
    total_hits = reuses + cache_hits
    avg_hits = round(total_hits / cache_entries, 2) if cache_entries > 0 else 0.0
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
    page = max(1, min(page, max_page))
    per_page = max(1, min(per_page, max_page))

    filters = []
    if cursor:
        filters.append(DeepAnalysis.id < cursor)
    if status_filter:
        filters.append(DeepAnalysis.status == status_filter)
    if engine_filter:
        filters.append(DeepAnalysis.engine == engine_filter)

    count_query = select(func.count()).select_from(DeepAnalysis)
    if status_filter:
        count_query = count_query.where(DeepAnalysis.status == status_filter)
    if engine_filter:
        count_query = count_query.where(DeepAnalysis.engine == engine_filter)
    total = (await session.execute(count_query)).scalar() or 0
    total_pages = max(1, (total + per_page - 1) // per_page)

    query = (
        select(DeepAnalysis, WebhookEvent)
        .outerjoin(WebhookEvent, WebhookEvent.id == DeepAnalysis.webhook_event_id)
        .order_by(DeepAnalysis.id.desc())
    )
    for condition in filters:
        query = query.where(condition)
    if not cursor:
        query = query.offset((page - 1) * per_page)
    query = query.limit(per_page)

    res = await session.execute(query)
    rows = res.all()
    items = []
    for rec, evt in rows:
        item = deep_analysis_to_dict(rec)
        item["source"] = evt.source if evt else None
        item["is_duplicate"] = evt.is_duplicate if evt else False
        item["beyond_window"] = False
        items.append(item)
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

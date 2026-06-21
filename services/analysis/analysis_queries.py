"""Read-side queries for AI usage and deep-analysis records."""

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utcnow
from db.session import count_with_timeout
from models import AIUsageLog, DeepAnalysis, WebhookEvent
from schemas.analysis import deep_analysis_to_summary_dict
from services.pagination import apply_cursor_window, clamp_page_params, trim_cursor_window


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

    # Sum all routes that avoid re-calling the LLM; keep the legacy route name for backward compatibility with historical data.
    reuses = (
        route_breakdown.get("reuse", 0)
        + route_breakdown.get("redis_reuse", 0)
        + route_breakdown.get("db_reuse", 0)
        + route_breakdown.get("rechain", 0)
    )
    cache_hits = route_breakdown.get("cache", 0)
    total_hits = reuses + cache_hits
    avg_hits = round(total_hits / cache_entries, 2) if cache_entries > 0 else 0.0
    hit_rate = round(total_hits / max(total, 1) * 100, 2)

    ai_calls = route_breakdown.get("ai", 0)
    avg_cost_per_ai_call = total_cost / ai_calls if ai_calls > 0 else 0.0
    saved_estimate = round(total_hits * avg_cost_per_ai_call, 6)

    trend = await _ai_usage_trend(session, start_time)

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
        "trend": trend,
    }


async def _ai_usage_trend(session: AsyncSession, start_time: datetime) -> list[dict[str, Any]]:
    """Per-day AI usage series since ``start_time`` (for the cost trend chart).

    Groups AIUsageLog by calendar day: total calls, AI vs rule calls, tokens,
    cost. ``func.date`` works on both PostgreSQL and the SQLite test shim. Days
    with no usage are simply absent (the chart connects what exists).
    """
    day = func.date(AIUsageLog.timestamp)
    rows = (
        await session.execute(
            select(
                day.label("d"),
                func.count(AIUsageLog.id),
                func.coalesce(func.sum(AIUsageLog.tokens_in + AIUsageLog.tokens_out), 0),
                func.coalesce(func.sum(AIUsageLog.cost_estimate), 0.0),
                func.sum(case((AIUsageLog.route_type == "ai", 1), else_=0)),
                func.sum(case((AIUsageLog.route_type == "rule", 1), else_=0)),
            )
            .filter(AIUsageLog.timestamp >= start_time)
            .group_by(day)
            .order_by(day)
        )
    ).all()
    return [
        {
            "time": str(r[0]),
            "total_calls": int(r[1] or 0),
            "ai_calls": int(r[4] or 0),
            "rule_calls": int(r[5] or 0),
            "tokens": int(r[2] or 0),
            "cost": float(r[3] or 0.0),
        }
        for r in rows
    ]


async def get_deep_analysis_list(
    session: AsyncSession,
    page: int = 1,
    per_page: int = 20,
    cursor: int | None = None,
    status_filter: str = "",
    engine_filter: str = "",
    max_page: int = 500,
) -> dict[str, Any]:
    page, per_page = clamp_page_params(page, per_page, max_page=max_page)

    filters = []
    if status_filter:
        filters.append(DeepAnalysis.status == status_filter)
    if engine_filter:
        filters.append(DeepAnalysis.engine == engine_filter)

    count_query = select(func.count()).select_from(DeepAnalysis)
    if status_filter:
        count_query = count_query.where(DeepAnalysis.status == status_filter)
    if engine_filter:
        count_query = count_query.where(DeepAnalysis.engine == engine_filter)
    total = await count_with_timeout(session, count_query) or 0
    total_pages = max(1, (total + per_page - 1) // per_page)

    # Join only the two WebhookEvent scalars the list needs (source,
    # is_duplicate) instead of the whole entity, which would also pull the
    # event's heavy JSONB columns (raw_payload, headers, parsed_data,
    # ai_analysis) for every joined row.
    query = (
        select(DeepAnalysis, WebhookEvent.source, WebhookEvent.is_duplicate)
        .outerjoin(WebhookEvent, WebhookEvent.id == DeepAnalysis.webhook_event_id)
        .order_by(DeepAnalysis.id.desc())
    )
    for condition in filters:
        query = query.where(condition)
    query = apply_cursor_window(query, DeepAnalysis.id, page=page, page_size=per_page, cursor=cursor)

    res = await session.execute(query)
    page_window = trim_cursor_window(res.all(), per_page, lambda row: row[0].id)

    items = []
    for rec, source, is_duplicate in page_window.rows:
        # Lightweight list item: a cheap summary preview, no full normalized
        # report and no raw analysis_result blob (fetched on demand via the
        # detail endpoint when a row is expanded).
        item = deep_analysis_to_summary_dict(rec)
        item["source"] = source
        item["is_duplicate"] = bool(is_duplicate) if is_duplicate is not None else False
        items.append(item)
    return {
        "items": items,
        "per_page": per_page,
        "page": page,
        "total": total,
        "total_pages": total_pages,
        "next_cursor": page_window.next_cursor,
        "has_more": page_window.has_more,
    }


async def get_deep_analyses_for_webhook(
    session: AsyncSession, webhook_id: int, *, limit: int = 50
) -> list[DeepAnalysis]:
    # Bound the result + per-row normalization. A webhook normally has only a
    # few deep analyses, but a runaway re-analysis loop could accumulate many;
    # cap so this endpoint can't normalize an unbounded set.
    stmt = (
        select(DeepAnalysis)
        .filter_by(webhook_event_id=webhook_id)
        .order_by(DeepAnalysis.created_at.desc())
        .limit(max(1, limit))
    )
    res = await session.execute(stmt)
    return list(res.scalars().all())

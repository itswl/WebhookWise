import math

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from crud.helpers import count_with_timeout
from models import DeepAnalysis, WebhookEvent


async def get_deep_analysis_list(
    session: AsyncSession,
    page: int = 1,
    per_page: int = 20,
    cursor: int | None = None,
    status_filter: str = "",
    engine_filter: str = "",
    max_page: int = 500,
):
    per_page = max(1, min(per_page, 100))
    has_filters = bool(status_filter or engine_filter)

    if not has_filters:
        try:
            estimate_result = await session.execute(
                text("SELECT reltuples::bigint FROM pg_class WHERE relname = 'deep_analyses'")
            )
            estimate = estimate_result.scalar()
            if estimate is not None and estimate > 100000:
                total = int(estimate)
            else:
                total_query = select(func.count()).select_from(DeepAnalysis)
                total = await count_with_timeout(session, total_query)
        except Exception:
            total_query = select(func.count()).select_from(DeepAnalysis)
            total = await count_with_timeout(session, total_query)
    else:
        total_query = select(func.count()).select_from(DeepAnalysis)
        if status_filter:
            total_query = total_query.filter(DeepAnalysis.status == status_filter)
        if engine_filter:
            total_query = total_query.filter(DeepAnalysis.engine == engine_filter)
        total = await count_with_timeout(session, total_query)

    query = (
        select(
            DeepAnalysis,
            WebhookEvent.source,
            WebhookEvent.is_duplicate,
            WebhookEvent.beyond_window,
        )
        .outerjoin(WebhookEvent, WebhookEvent.id == DeepAnalysis.webhook_event_id)
        .order_by(DeepAnalysis.id.desc())
    )

    if cursor:
        query = query.filter(DeepAnalysis.id < cursor)
    if status_filter:
        query = query.filter(DeepAnalysis.status == status_filter)
    if engine_filter:
        query = query.filter(DeepAnalysis.engine == engine_filter)

    offset = 0
    if not cursor:
        if page > max_page:
            raise ValueError(f"page 超过上限 {max_page}，请使用 cursor 游标分页")
        offset = (page - 1) * per_page
        query = query.offset(offset)

    result = await session.execute(query.limit(per_page))
    rows = result.all()

    next_cursor = rows[-1][0].id if rows else None

    total_pages = math.ceil(total / per_page) if total is not None and total > 0 else (1 if total is not None else None)

    items = []
    for record, source, is_duplicate, beyond_window in rows:
        d = record.to_dict()
        d["source"] = source
        d["is_duplicate"] = bool(is_duplicate) if is_duplicate is not None else False
        d["beyond_window"] = bool(beyond_window) if beyond_window is not None else False
        items.append(d)

    return {
        "total": total,
        "total_pages": total_pages,
        "page": page if not cursor else None,
        "per_page": per_page,
        "next_cursor": next_cursor,
        "items": items,
    }


async def get_deep_analyses_for_webhook(session: AsyncSession, webhook_id: int):
    result = await session.execute(
        select(DeepAnalysis).filter_by(webhook_event_id=webhook_id).order_by(DeepAnalysis.created_at.desc())
    )
    records = result.scalars().all()
    return [r.to_dict() for r in records]

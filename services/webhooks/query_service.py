"""Webhook 查询服务：列表投影、分页与恢复视图。"""

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import WebhookEvent

_PrevEvent = WebhookEvent.__table__.alias("prev_evt")
_prev_ts_subq = (
    select(_PrevEvent.c.timestamp)
    .where(_PrevEvent.c.id == WebhookEvent.prev_alert_id)
    .correlate(WebhookEvent.__table__)
    .scalar_subquery()
    .label("prev_alert_timestamp")
)

LEGACY_STUCK_STATUSES = ["received", "analyzing", "retry", "failed"]

_SUMMARY_COLUMNS = [
    WebhookEvent.id,
    WebhookEvent.request_id,
    WebhookEvent.source,
    WebhookEvent.client_ip,
    WebhookEvent.timestamp,
    WebhookEvent.importance,
    WebhookEvent.is_duplicate,
    WebhookEvent.duplicate_of,
    WebhookEvent.duplicate_count,
    WebhookEvent.beyond_window,
    WebhookEvent.forward_status,
    WebhookEvent.ai_analysis,
    WebhookEvent.parsed_data,
    WebhookEvent.created_at,
    WebhookEvent.prev_alert_id,
    _prev_ts_subq,
]


def _row_to_summary_dict(row: Any) -> dict[str, Any]:
    from adapters.summary_extractors import extract_summary_fields

    ai_analysis = row.ai_analysis
    beyond_window, is_dup = row.beyond_window, row.is_duplicate
    prev_ts = getattr(row, "prev_alert_timestamp", None)
    return {
        "id": row.id,
        "request_id": row.request_id,
        "source": row.source,
        "client_ip": row.client_ip,
        "timestamp": row.timestamp.isoformat() if row.timestamp else None,
        "importance": row.importance,
        "is_duplicate": is_dup,
        "duplicate_of": row.duplicate_of,
        "duplicate_count": row.duplicate_count,
        "beyond_window": beyond_window,
        "forward_status": row.forward_status,
        "summary": ai_analysis.get("summary", "") if ai_analysis else None,
        "alert_info": extract_summary_fields(row.source, row.parsed_data),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "prev_alert_id": row.prev_alert_id,
        "prev_alert_timestamp": prev_ts.isoformat() if prev_ts else None,
        "beyond_time_window": beyond_window,
        "is_within_window": is_dup and not beyond_window,
        "duplicate_type": ("beyond_window" if beyond_window else "within_window") if is_dup else "new",
    }


async def list_webhook_summaries(
    session: AsyncSession,
    *,
    cursor_id: int | None = None,
    importance: str = "",
    source: str = "",
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[dict[str, Any]], bool, int | None]:
    query = select(*_SUMMARY_COLUMNS)
    if cursor_id is not None:
        query = query.where(WebhookEvent.id < cursor_id)
    if importance:
        query = query.where(WebhookEvent.importance == importance)
    if source:
        query = query.where(WebhookEvent.source == source)
    if cursor_id is None and page > 1:
        query = query.offset((page - 1) * page_size)
    query = query.order_by(WebhookEvent.id.desc()).limit(page_size + 1)
    result = await session.execute(query)
    rows = result.all()
    has_more = len(rows) > page_size
    if has_more:
        rows = rows[:page_size]
    items = [_row_to_summary_dict(r) for r in rows]
    return items, has_more, (rows[-1].id if has_more and rows else None)


async def list_webhook_summaries_cursor(
    session: AsyncSession, *, cursor_id: int | None = None, importance: str = "", source: str = "", limit: int = 200
) -> tuple[list[dict[str, Any]], bool, int | None]:
    query = select(*_SUMMARY_COLUMNS)
    if importance:
        query = query.where(WebhookEvent.importance == importance)
    if source:
        query = query.where(WebhookEvent.source == source)
    if cursor_id is not None:
        query = query.where(WebhookEvent.id < cursor_id)
    query = query.order_by(WebhookEvent.timestamp.desc(), WebhookEvent.id.desc()).limit(limit)
    result = await session.execute(query)
    rows = result.all()
    has_more = len(rows) == limit
    items = [_row_to_summary_dict(r) for r in rows]
    return items, has_more, (rows[-1].id if has_more and rows else None)


async def list_dead_letters(session: AsyncSession, page: int = 1, page_size: int = 20) -> list[dict[str, Any]]:
    stmt = (
        select(
            WebhookEvent.id,
            WebhookEvent.source,
            WebhookEvent.timestamp,
            WebhookEvent.created_at,
            WebhookEvent.alert_hash,
            WebhookEvent.importance,
            WebhookEvent.retry_count,
            WebhookEvent.processing_status,
        )
        .where(WebhookEvent.processing_status == "dead_letter")
        .order_by(WebhookEvent.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    result = await session.execute(stmt)
    rows: list[dict[str, Any]] = []
    for row in result.all():
        d = dict(row._mapping)
        for k in ("timestamp", "created_at"):
            if isinstance(d.get(k), datetime):
                d[k] = d[k].isoformat()
        rows.append(d)
    return rows


async def count_dead_letters(session: AsyncSession) -> int | None:
    from db.session import count_with_timeout

    stmt = select(func.count()).select_from(WebhookEvent).where(WebhookEvent.processing_status == "dead_letter")
    return await count_with_timeout(session, stmt)


async def list_stuck_events(
    session: AsyncSession, *, statuses: list[str] | None = None, older_than_seconds: int = 300, limit: int = 50
) -> list[dict[str, Any]]:
    threshold = datetime.now() - timedelta(seconds=max(0, older_than_seconds))
    stmt = (
        select(
            WebhookEvent.id,
            WebhookEvent.source,
            WebhookEvent.created_at,
            WebhookEvent.updated_at,
            WebhookEvent.retry_count,
            WebhookEvent.processing_status,
        )
        .where(
            WebhookEvent.processing_status.in_(statuses or LEGACY_STUCK_STATUSES),
            WebhookEvent.created_at < threshold,
        )
        .order_by(WebhookEvent.created_at.asc())
        .limit(limit)
    )
    res = await session.execute(stmt)
    rows = []
    for row in res.all():
        d = dict(row._mapping)
        for k in ("created_at", "updated_at"):
            if isinstance(d.get(k), datetime):
                d[k] = d[k].isoformat()
        rows.append(d)
    return rows

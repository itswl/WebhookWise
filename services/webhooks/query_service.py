"""Webhook 查询服务：列表投影、分页与死信视图。"""

from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import ForwardOutbox, WebhookEvent

_PrevEvent = WebhookEvent.__table__.alias("prev_evt")
_prev_ts_subq = (
    select(_PrevEvent.c.timestamp)
    .where(_PrevEvent.c.id == WebhookEvent.prev_alert_id)
    .correlate(WebhookEvent.__table__)
    .scalar_subquery()
    .label("prev_alert_timestamp")
)

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
    WebhookEvent.forward_status,
    WebhookEvent.ai_analysis,
    WebhookEvent.parsed_data,
    WebhookEvent.created_at,
    WebhookEvent.prev_alert_id,
    _prev_ts_subq,
]


def _row_to_summary_dict(row: Any) -> dict[str, Any]:
    from schemas.webhook import mongodb_summary_fields

    ai_analysis = row.ai_analysis
    is_dup = row.is_duplicate
    prev_ts = getattr(row, "prev_alert_timestamp", None)
    duplicate_type = "within_window" if is_dup else "new"
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
        "beyond_window": False,
        "forward_status": row.forward_status,
        "summary": ai_analysis.get("summary", "") if ai_analysis else None,
        "alert_info": mongodb_summary_fields(row.parsed_data) if row.source == "mongodb" else {},
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "prev_alert_id": row.prev_alert_id,
        "prev_alert_timestamp": prev_ts.isoformat() if prev_ts else None,
        "is_within_window": bool(is_dup),
        "duplicate_type": duplicate_type,
    }


def _merge_forward_status(current: str | None, outbox_status: str | None) -> str | None:
    if outbox_status == "sent":
        return "sent"
    if outbox_status in {"exhausted", "expired"} and current in {"queued", "pending", "retrying", None}:
        return "failed"
    return current


async def _apply_outbox_forward_statuses(session: AsyncSession, items: list[dict[str, Any]]) -> None:
    event_ids = [int(item["id"]) for item in items if item.get("id")]
    if not event_ids:
        return
    event_id_set = set(event_ids)

    query = select(ForwardOutbox.webhook_event_id, ForwardOutbox.original_event_id, ForwardOutbox.status).where(
        or_(ForwardOutbox.webhook_event_id.in_(event_ids), ForwardOutbox.original_event_id.in_(event_ids))
    )
    rows = (await session.execute(query)).all()
    status_by_event_id: dict[int, str | None] = {}
    for row in rows:
        for event_id in (row.webhook_event_id, row.original_event_id):
            if event_id in event_id_set:
                event_int = int(event_id)
                status_by_event_id[event_int] = _merge_forward_status(status_by_event_id.get(event_int), row.status)

    for item in items:
        event_id = int(item["id"])
        item["forward_status"] = _merge_forward_status(item.get("forward_status"), status_by_event_id.get(event_id))


async def list_webhook_summaries(
    session: AsyncSession,
    *,
    cursor: int | None = None,
    importance: str = "",
    source: str = "",
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[dict[str, Any]], bool, int | None]:
    query = select(*_SUMMARY_COLUMNS)
    if cursor is not None:
        query = query.where(WebhookEvent.id < cursor)
    if importance:
        query = query.where(WebhookEvent.importance == importance)
    if source:
        query = query.where(WebhookEvent.source == source)
    if cursor is None and page > 1:
        query = query.offset((page - 1) * page_size)
    query = query.order_by(WebhookEvent.id.desc()).limit(page_size + 1)
    result = await session.execute(query)
    rows = result.all()
    has_more = len(rows) > page_size
    if has_more:
        rows = rows[:page_size]
    items = [_row_to_summary_dict(r) for r in rows]
    await _apply_outbox_forward_statuses(session, items)
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

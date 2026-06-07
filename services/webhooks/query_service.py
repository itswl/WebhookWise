"""Webhook 查询服务：列表投影、分页与死信视图。"""

from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utc_isoformat
from models import ForwardOutbox, WebhookEvent
from schemas.webhook import WebhookEventSummary
from services.pagination import apply_cursor_window, trim_cursor_window

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
    WebhookEvent.created_at,
    WebhookEvent.prev_alert_id,
    _prev_ts_subq,
]


def _row_to_summary_dict(row: Any) -> dict[str, Any]:
    return WebhookEventSummary.model_validate(row).model_dump(mode="json")


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
    if importance:
        query = query.where(WebhookEvent.importance == importance)
    if source:
        query = query.where(WebhookEvent.source == source)
    query = query.order_by(WebhookEvent.id.desc())
    query = apply_cursor_window(query, WebhookEvent.id, page=page, page_size=page_size, cursor=cursor)
    result = await session.execute(query)
    page_window = trim_cursor_window(result.all(), page_size, lambda row: row.id)
    items = [_row_to_summary_dict(r) for r in page_window.rows]
    await _apply_outbox_forward_statuses(session, items)
    return items, page_window.has_more, page_window.next_cursor


async def list_dead_letters(
    session: AsyncSession,
    page: int = 1,
    page_size: int = 20,
    source: str | None = None,
    status: str = "dead_letter",
    search: str | None = None,
    time_from: str | None = None,
    time_to: str | None = None,
) -> list[dict[str, Any]]:
    stmt = select(
        WebhookEvent.id,
        WebhookEvent.source,
        WebhookEvent.timestamp,
        WebhookEvent.created_at,
        WebhookEvent.alert_hash,
        WebhookEvent.importance,
        WebhookEvent.retry_count,
        WebhookEvent.processing_status,
        WebhookEvent.failure_reason,
        WebhookEvent.error_message,
    ).where(WebhookEvent.processing_status == status)

    if source:
        stmt = stmt.where(WebhookEvent.source == source)
    if search:
        like_pattern = f"%{search}%"
        stmt = stmt.where(
            or_(
                WebhookEvent.error_message.ilike(like_pattern),
                WebhookEvent.failure_reason.ilike(like_pattern),
            )
        )
    if time_from:
        try:
            from core.datetime_utils import parse_utc_datetime
            stmt = stmt.where(WebhookEvent.timestamp >= parse_utc_datetime(time_from))
        except (ValueError, TypeError):
            pass
    if time_to:
        try:
            from core.datetime_utils import parse_utc_datetime
            stmt = stmt.where(WebhookEvent.timestamp <= parse_utc_datetime(time_to))
        except (ValueError, TypeError):
            pass

    stmt = stmt.order_by(WebhookEvent.id.desc()).offset((page - 1) * page_size).limit(page_size)
    result = await session.execute(stmt)
    rows: list[dict[str, Any]] = []
    for row in result.all():
        d = dict(row._mapping)
        for k in ("timestamp", "created_at"):
            if isinstance(d.get(k), datetime):
                d[k] = utc_isoformat(d[k])
        rows.append(d)
    return rows


async def count_dead_letters(
    session: AsyncSession,
    source: str | None = None,
    status: str = "dead_letter",
    search: str | None = None,
    time_from: str | None = None,
    time_to: str | None = None,
) -> int | None:
    from db.session import count_with_timeout

    stmt = select(func.count()).select_from(WebhookEvent).where(WebhookEvent.processing_status == status)
    if source:
        stmt = stmt.where(WebhookEvent.source == source)
    if search:
        like_pattern = f"%{search}%"
        stmt = stmt.where(
            or_(
                WebhookEvent.error_message.ilike(like_pattern),
                WebhookEvent.failure_reason.ilike(like_pattern),
            )
        )
    if time_from:
        try:
            from core.datetime_utils import parse_utc_datetime
            stmt = stmt.where(WebhookEvent.timestamp >= parse_utc_datetime(time_from))
        except (ValueError, TypeError):
            pass
    if time_to:
        try:
            from core.datetime_utils import parse_utc_datetime
            stmt = stmt.where(WebhookEvent.timestamp <= parse_utc_datetime(time_to))
        except (ValueError, TypeError):
            pass
    return await count_with_timeout(session, stmt)


async def get_dead_letter_detail(session: AsyncSession, event_id: int) -> dict[str, Any] | None:
    """Get a single dead letter event with full detail including payload."""
    stmt = select(WebhookEvent).where(WebhookEvent.id == event_id)
    result = await session.execute(stmt)
    event = result.scalar_one_or_none()
    if event is None:
        return None

    # Load the raw payload
    from services.webhooks.repository import load_event_payload
    parsed, raw_body = await load_event_payload(event)

    detail = {
        "id": event.id,
        "source": event.source,
        "request_id": event.request_id,
        "client_ip": event.client_ip,
        "timestamp": utc_isoformat(event.timestamp) if event.timestamp else None,
        "created_at": utc_isoformat(event.created_at) if event.created_at else None,
        "processing_status": event.processing_status,
        "failure_reason": event.failure_reason,
        "error_message": event.error_message,
        "retry_count": event.retry_count,
        "importance": event.importance,
        "alert_hash": event.alert_hash,
        "dedup_key": event.dedup_key,
        "forward_status": event.forward_status,
        "parsed_data": event.parsed_data,
        "raw_payload": parsed,
    }
    return detail

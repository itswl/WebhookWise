"""Webhook query service: list projections, pagination, and dead-letter views."""

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utc_isoformat, utcnow
from models import ForwardOutbox, WebhookEvent
from services.pagination import apply_cursor_window, trim_cursor_window

# Time-window presets for the alert list filter. "all" (or unknown) → no bound.
_WINDOW_DELTAS = {"today": timedelta(days=1), "7d": timedelta(days=7), "30d": timedelta(days=30)}


def window_to_time_from(window: str) -> datetime | None:
    """Map a window preset (today / 7d / 30d / all) to a lower time bound."""
    delta = _WINDOW_DELTAS.get(window)
    return (utcnow() - delta) if delta else None

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
    # Project only ai_analysis->>'summary' instead of loading the whole JSONB
    # blob per row just to read one string (works on PostgreSQL JSONB and the
    # SQLite-JSON test shim alike).
    WebhookEvent.ai_analysis["summary"].astext.label("summary"),
    WebhookEvent.created_at,
    WebhookEvent.prev_alert_id,
    _prev_ts_subq,
]


def _row_to_summary_dict(row: Any) -> dict[str, Any]:
    # Build the response dict directly from the trusted DB projection instead of
    # per-row Pydantic validate+dump (the hot cost on large list pages). Mirrors
    # WebhookEventSummary's derived fields.
    is_duplicate = bool(row.is_duplicate)
    return {
        "id": row.id,
        "request_id": row.request_id,
        "source": row.source,
        "client_ip": row.client_ip,
        "timestamp": utc_isoformat(row.timestamp) if row.timestamp is not None else None,
        "importance": row.importance,
        "is_duplicate": is_duplicate,
        "duplicate_of": row.duplicate_of,
        "duplicate_count": row.duplicate_count,
        "duplicate_type": "within_window" if is_duplicate else "new",
        "forward_status": row.forward_status,
        "summary": row.summary,
        "created_at": utc_isoformat(row.created_at) if row.created_at is not None else None,
        "prev_alert_id": row.prev_alert_id,
        "prev_alert_timestamp": (
            utc_isoformat(row.prev_alert_timestamp) if row.prev_alert_timestamp is not None else None
        ),
        "is_within_window": is_duplicate,
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


def _apply_summary_filters(query: Any, *, importance: str, source: str, time_from: datetime | None) -> Any:
    if importance:
        query = query.where(WebhookEvent.importance == importance)
    if source:
        query = query.where(WebhookEvent.source == source)
    if time_from is not None:
        query = query.where(WebhookEvent.timestamp >= time_from)
    return query


async def list_webhook_summaries(
    session: AsyncSession,
    *,
    cursor: int | None = None,
    importance: str = "",
    source: str = "",
    time_from: datetime | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[dict[str, Any]], bool, int | None]:
    query = _apply_summary_filters(select(*_SUMMARY_COLUMNS), importance=importance, source=source, time_from=time_from)
    query = query.order_by(WebhookEvent.id.desc())
    query = apply_cursor_window(query, WebhookEvent.id, page=page, page_size=page_size, cursor=cursor)
    result = await session.execute(query)
    page_window = trim_cursor_window(result.all(), page_size, lambda row: row.id)
    items = [_row_to_summary_dict(r) for r in page_window.rows]
    await _apply_outbox_forward_statuses(session, items)
    return items, page_window.has_more, page_window.next_cursor


async def count_webhook_summaries(
    session: AsyncSession,
    *,
    importance: str = "",
    source: str = "",
    time_from: datetime | None = None,
) -> int | None:
    """Total event count matching the same filters (for the list's real total).

    Uses count_with_timeout so a slow count degrades to None ("unknown") rather
    than stalling the page; the API surfaces None as an unknown total.
    """
    from db.session import count_with_timeout

    stmt = _apply_summary_filters(
        select(func.count()).select_from(WebhookEvent), importance=importance, source=source, time_from=time_from
    )
    return await count_with_timeout(session, stmt)


def _dead_letter_base_query(
    *,
    source: str | None = None,
    search: str | None = None,
    time_from: datetime | None = None,
    time_to: datetime | None = None,
) -> Any:
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
    ).where(WebhookEvent.processing_status == "dead_letter")
    if source:
        stmt = stmt.where(WebhookEvent.source == source)
    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(or_(WebhookEvent.error_message.ilike(pattern), WebhookEvent.failure_reason.ilike(pattern)))
    if time_from is not None:
        stmt = stmt.where(WebhookEvent.timestamp >= time_from)
    if time_to is not None:
        stmt = stmt.where(WebhookEvent.timestamp <= time_to)
    return stmt


async def list_dead_letters(
    session: AsyncSession,
    page: int = 1,
    page_size: int = 20,
    *,
    source: str | None = None,
    search: str | None = None,
    time_from: datetime | None = None,
    time_to: datetime | None = None,
) -> list[dict[str, Any]]:
    stmt = (
        _dead_letter_base_query(source=source, search=search, time_from=time_from, time_to=time_to)
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
                d[k] = utc_isoformat(d[k])
        rows.append(d)
    return rows


async def count_dead_letters(
    session: AsyncSession,
    *,
    source: str | None = None,
    search: str | None = None,
    time_from: datetime | None = None,
    time_to: datetime | None = None,
) -> int | None:
    from db.session import count_with_timeout

    stmt = (
        select(func.count())
        .select_from(WebhookEvent)
        .where(WebhookEvent.processing_status == "dead_letter")
    )
    if source:
        stmt = stmt.where(WebhookEvent.source == source)
    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(or_(WebhookEvent.error_message.ilike(pattern), WebhookEvent.failure_reason.ilike(pattern)))
    if time_from is not None:
        stmt = stmt.where(WebhookEvent.timestamp >= time_from)
    if time_to is not None:
        stmt = stmt.where(WebhookEvent.timestamp <= time_to)
    return await count_with_timeout(session, stmt)


async def get_dead_letter_detail(session: AsyncSession, event_id: int) -> dict[str, Any] | None:
    from services.webhooks.repository import load_event_payload

    stmt = select(WebhookEvent).where(WebhookEvent.id == event_id, WebhookEvent.processing_status == "dead_letter")
    event = (await session.execute(stmt)).scalar_one_or_none()
    if event is None:
        return None
    parsed_payload, raw_body = await load_event_payload(event)
    return {
        "id": event.id,
        "source": event.source,
        "request_id": event.request_id,
        "client_ip": event.client_ip,
        "timestamp": utc_isoformat(event.timestamp),
        "created_at": utc_isoformat(event.created_at),
        "processing_status": event.processing_status,
        "retry_count": event.retry_count,
        "failure_reason": event.failure_reason,
        "error_message": event.error_message,
        "importance": event.importance,
        "alert_hash": event.alert_hash,
        "dedup_key": event.dedup_key,
        "forward_status": event.forward_status,
        "headers": event.headers or {},
        "parsed_data": event.parsed_data,
        "payload": parsed_payload,
        "raw_body": raw_body,
    }

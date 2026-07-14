"""Read-side queries for forwarding outbox records."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from core.datetime_utils import utc_isoformat
from core.logger import mask_url
from db.session import count_with_timeout, session_scope
from models import ForwardOutbox
from services.pagination import apply_cursor_window, clamp_page_params, trim_cursor_window


async def list_outbox_records(
    *,
    page: int = 1,
    page_size: int = 20,
    cursor: int | None = None,
    status: str = "",
    event_type: str = "",
    session_scope_factory: Any | None = None,
    count_with_timeout_fn: Any | None = None,
) -> dict[str, Any]:
    """Return paginated forwarding outbox records for admin/API screens."""
    page, page_size = clamp_page_params(page, page_size, max_page=100, max_page_size=200)

    filters = []
    if status:
        filters.append(ForwardOutbox.status == status)
    if event_type:
        filters.append(ForwardOutbox.event_type == event_type)

    scope = session_scope_factory or session_scope
    count_fn = count_with_timeout_fn or count_with_timeout
    async with scope() as session:
        # The total only matters when opening the list; cursor scrolls ("load
        # more") should not re-pay a COUNT over the table each page. None means
        # "unchanged since page 1" to the client.
        total: int | None = None
        if cursor is None:
            count_q = select(func.count()).select_from(ForwardOutbox)
            for condition in filters:
                count_q = count_q.where(condition)
            total = await count_fn(session, count_q) or 0

        # Project only the columns the list needs. Selecting the full entity
        # would also pull 4 large JSONB columns (forward_data, analysis_result,
        # formatted_payload, response_data) that are never returned here.
        query = select(
            ForwardOutbox.id,
            ForwardOutbox.webhook_event_id,
            ForwardOutbox.original_event_id,
            ForwardOutbox.rule_name,
            ForwardOutbox.target_type,
            ForwardOutbox.target_url,
            ForwardOutbox.target_name,
            ForwardOutbox.event_type,
            ForwardOutbox.status,
            ForwardOutbox.attempts,
            ForwardOutbox.max_attempts,
            ForwardOutbox.next_attempt_at,
            ForwardOutbox.last_attempt_at,
            ForwardOutbox.sent_at,
            ForwardOutbox.last_error,
            ForwardOutbox.is_periodic_reminder,
            ForwardOutbox.created_at,
        ).order_by(ForwardOutbox.id.desc())
        for condition in filters:
            query = query.where(condition)
        query = apply_cursor_window(query, ForwardOutbox.id, page=page, page_size=page_size, cursor=cursor)
        page_window = trim_cursor_window((await session.execute(query)).all(), page_size, lambda row: row.id)

        items = [
            {
                "id": row.id,
                "webhook_event_id": row.webhook_event_id,
                "original_event_id": row.original_event_id,
                "rule_name": row.rule_name,
                "target_type": row.target_type,
                "target_url": _mask_url_for_display(row.target_url or ""),
                "target_name": row.target_name,
                "event_type": row.event_type,
                "status": row.status,
                "attempts": row.attempts,
                "max_attempts": row.max_attempts,
                "next_attempt_at": utc_isoformat(row.next_attempt_at),
                "last_attempt_at": utc_isoformat(row.last_attempt_at),
                "sent_at": utc_isoformat(row.sent_at),
                "last_error": (row.last_error or "")[:200],
                "is_periodic_reminder": row.is_periodic_reminder,
                "created_at": utc_isoformat(row.created_at),
            }
            for row in page_window.rows
        ]

    return {
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": (max(1, (total + page_size - 1) // page_size) if total else 1) if total is not None else None,
        "next_cursor": page_window.next_cursor,
        "has_more": page_window.has_more,
    }


def _mask_url_for_display(url: str) -> str:
    if not url:
        return ""
    return mask_url(url)

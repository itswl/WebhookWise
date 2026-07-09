"""Alert incident timeline: walks the dedup chain and noise-reduction
related-alert graph to build a chronological timeline for a given alert.

Queries are read-only over existing data — no schema change, no new instruments.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utc_isoformat
from core.logger import get_logger
from models import WebhookEvent

logger = get_logger("webhooks.timeline")

_MAX_TIMELINE_EVENTS = 50


async def build_alert_timeline(session: AsyncSession, event_id: int) -> dict[str, Any]:
    """Return a chronological timeline of alerts related to *event_id*.

    Walks three edges:
    1. The noise-reduction graph (``related_alert_ids`` + ``root_cause_event_id``
       from ``ai_analysis.noise_reduction``).
    2. The dedup chain backwards (``prev_alert_id``).
    3. The dedup chain forwards (other events whose ``duplicate_of == event_id``
       or ``prev_alert_id == event_id``).

    All fetched events are projected as summaries (no raw payload / headers) then
    sorted by ``timestamp`` ascending. Returns ``{"anchor": {...}, "events": [...]}``
    with the anchor event pinned.
    """
    anchor = await session.get(WebhookEvent, event_id)
    if anchor is None:
        return {"anchor": None, "events": []}

    related_ids: set[int] = {event_id}
    _walk_noise_graph(anchor, related_ids)
    if related_ids:
        await _walk_dedup_chain(session, related_ids)

    # Fetch all events in one batch.
    id_list = [rid for rid in related_ids if rid > 0]
    if not id_list:
        return {"anchor": _event_timeline_row(anchor), "events": []}

    rows = list(
        (await session.execute(select(WebhookEvent).where(WebhookEvent.id.in_(id_list))))
        .scalars()
        .all()
    )

    # Sort chronologically ascending (earliest first).
    rows.sort(key=lambda r: r.timestamp)

    timeline = [_event_timeline_row(r) for r in rows]
    anchor_row = _event_timeline_row(anchor)
    return {"anchor": anchor_row, "events": timeline}


def _walk_noise_graph(anchor: WebhookEvent, seen: set[int]) -> None:
    """Collect IDs reachable through the noise-reduction analysis."""
    analysis = anchor.ai_analysis or {}
    noise = analysis.get("noise_reduction")
    if not isinstance(noise, dict):
        return

    root_id = noise.get("root_cause_event_id")
    if isinstance(root_id, int) and root_id > 0:
        seen.add(root_id)

    related = noise.get("related_alert_ids")
    if isinstance(related, (list, tuple)):
        for rid in related:
            if isinstance(rid, int) and rid > 0:
                seen.add(int(rid))


async def _walk_dedup_chain(session: AsyncSession, seen: set[int]) -> None:
    """Walk the dedup graph one hop in each direction.

    Backward: prev_alert_id of any event already in ``seen``.
    Forward: other events whose prev_alert_id or duplicate_of points into
    ``seen``. Bounded by _MAX_TIMELINE_EVENTS so a long chain cannot blow up.
    """
    current_ids = set(seen)
    if len(current_ids) >= _MAX_TIMELINE_EVENTS:
        return

    # Backward: fetch prev_alert_id for events in the set.
    back_rows = (
        await session.execute(
            select(WebhookEvent.id, WebhookEvent.prev_alert_id).where(
                WebhookEvent.id.in_(list(current_ids))
            )
        )
    ).all()
    for row in back_rows:
        if isinstance(row.prev_alert_id, int) and row.prev_alert_id > 0:
            seen.add(row.prev_alert_id)

    # Forward: events whose prev_alert_id or duplicate_of points into the seed set.
    seed = list(seen)[:_MAX_TIMELINE_EVENTS]
    fwd_rows: list[Any] = list(
        (
            await session.execute(
                select(WebhookEvent.id).where(
                    (WebhookEvent.prev_alert_id.in_(seed)) | (WebhookEvent.duplicate_of.in_(seed))
                )
            )
        ).all()
    )
    for row in fwd_rows:
        seen.add(row.id)


def _event_timeline_row(event: WebhookEvent) -> dict[str, Any]:
    """A compact event row for the timeline view (mirrors the summary projection)."""
    summary = ""
    analysis = event.ai_analysis or {}
    if isinstance(analysis, dict):
        summary = str(analysis.get("summary", "") or "")[:200]
    # Extract noise-reduction context (root_cause_event_id, related_alert_ids) so
    # the frontend can draw causal arrows between related events.
    noise = analysis.get("noise_reduction", {}) if isinstance(analysis, dict) else {}
    root_cause_id = noise.get("root_cause_event_id") if isinstance(noise, dict) else None
    related_ids = noise.get("related_alert_ids", []) if isinstance(noise, dict) else []
    return {
        "id": event.id,
        "source": event.source or "unknown",
        "importance": event.importance or "unknown",
        "timestamp": utc_isoformat(event.timestamp),
        "summary": summary,
        "is_duplicate": bool(event.is_duplicate),
        "duplicate_of": event.duplicate_of,
        "prev_alert_id": event.prev_alert_id,
        "noise_root_cause_id": root_cause_id if isinstance(root_cause_id, int) else None,
        "noise_related_ids": (
            [int(rid) for rid in related_ids if isinstance(rid, int)]
            if isinstance(related_ids, (list, tuple))
            else []
        ),
        "processing_status": event.processing_status,
        "forward_status": event.forward_status,
    }

"""Incident grouping engine — clusters related alerts into operational incidents.

Runs as a periodic background task (like the forward-outbox scanner). On each
tick it:
1. Fetches recent events that aren't yet assigned to any incident.
2. Matches each event to an existing active incident (by source + time proximity).
3. Creates new incidents for unmatched events.
4. Closes incidents that have been quiet (no new member alerts) for a configured
   quiet window and triggers an LLM summary.

Pure read-modify-write over existing webhook_events — no hot-path impact.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utcnow
from core.logger import get_logger
from db.session import session_scope
from models import Incident, WebhookEvent

logger = get_logger("incidents.grouping")

# Matching window: events within this many minutes of each other can belong to
# the same incident (default 15 min).
_INCIDENT_WINDOW_MINUTES = 15

# Quiet window: if no new alerts arrive for this many minutes, the incident is
# considered "quiet" (ready for summarization).
_INCIDENT_QUIET_MINUTES = 10

# How far back to scan for unassigned events (minutes).
# Scan the last 72 h so the first run can backfill existing alerts even when
# the system has been quiet for a day or two (e.g. after a deploy or slow
# weekend). Subsequent ticks cover only the trailing window since the last
# assigned event, so this generous window is self-limiting.
_SCAN_LOOKBACK_MINUTES = 4320  # 72 h

_MAX_MEMBERS_PER_INCIDENT = 200
_MAX_INCIDENTS_PER_SCAN = 50


async def run_incident_grouping() -> dict[str, Any]:
    """One tick of the incident grouping scanner.

    Returns a summary dict for logging / metrics.
    """
    now = utcnow()
    lookback_cutoff = now - timedelta(minutes=_SCAN_LOOKBACK_MINUTES)
    window_cutoff = now - timedelta(minutes=_INCIDENT_WINDOW_MINUTES)

    async with session_scope() as session:
        # ── 1. Find unassigned events ──────────────────────────────────────
        # An event is "unassigned" if it's not in any existing incident's
        # member_ids array. We fetch all recent events and filter in Python
        # rather than doing a NOT EXISTS subquery (portable to SQLite tests).
        recent_events = list(
            (
                await session.execute(
                    select(WebhookEvent)
                    .where(WebhookEvent.timestamp >= lookback_cutoff)
                    .order_by(WebhookEvent.timestamp.asc())
                )
            )
            .scalars()
            .all()
        )
        if not recent_events:
            logger.debug("[Incidents] No recent events to group")
            return {"scanned": 0, "created": 0, "updated": 0, "closed": 0}

        # Collect all event IDs already assigned to any incident.
        assigned_ids: set[int] = set()
        assigned_rows = (
            await session.execute(
                select(Incident.member_ids).where(Incident.member_ids.isnot(None))
            )
        ).all()
        for row in assigned_rows:
            ids = row[0]
            if isinstance(ids, list):
                for mid in ids:
                    if isinstance(mid, int):
                        assigned_ids.add(mid)

        unassigned = [e for e in recent_events if e.id not in assigned_ids]
        if not unassigned:
            logger.debug("[Incidents] All recent events already assigned")
            return {"scanned": len(recent_events), "created": 0, "updated": 0, "closed": 0}

        # ── 2. Fetch active incidents ──────────────────────────────────────
        active_incidents = list(
            (
                await session.execute(
                    select(Incident)
                    .where(Incident.status == "active")
                    .order_by(Incident.started_at.asc())
                )
            )
            .scalars()
            .all()
        )

        # ── 3. Group unassigned events ─────────────────────────────────────
        created = 0
        updated = 0

        for event in unassigned:
            match = _find_matching_incident(event, active_incidents, now, window_cutoff)
            if match:
                _add_event_to_incident(match, event)
                updated += 1
            else:
                incident = _create_incident_from_event(event)
                session.add(incident)
                active_incidents.append(incident)
                created += 1

            if created + updated >= _MAX_INCIDENTS_PER_SCAN:
                break

        await session.flush()

        # ── 4. Close quiet incidents ───────────────────────────────────────
        closed = await _close_quiet_incidents(session, now)

        stats = {
            "scanned": len(unassigned),
            "created": created,
            "updated": updated,
            "closed": closed,
        }
        if created or updated or closed:
            logger.info(
                "[Incidents] Grouping tick: scanned=%d created=%d updated=%d closed=%d",
                stats["scanned"],
                stats["created"],
                stats["updated"],
                stats["closed"],
            )
        return stats


def _find_matching_incident(
    event: WebhookEvent, incidents: list[Incident], now: Any, window_cutoff: Any
) -> Incident | None:
    """Return the best active incident for *event*, or None.

    Matching criteria (all must be true):
    1. Same source.
    2. The incident's latest member timestamp is within the incident window.
    """
    for incident in incidents:
        if incident.status != "active":
            continue
        # Same source is the strongest clustering signal.
        if str(incident.source or "") != str(event.source or ""):
            continue
        # Time proximity: the last event in this incident must be recent enough.
        last_ts = _incident_last_timestamp(incident, event)
        if last_ts is not None and last_ts < window_cutoff:
            continue
        return incident
    return None


def _incident_last_timestamp(incident: Incident, fallback_event: WebhookEvent) -> Any | None:
    """Best-effort timestamp of the most recent member alert."""
    members = incident.member_ids or []
    if members:
        # The member_ids list is ordered (newest last), so the last element is the
        # most recent event id. We don't have timestamps in the array, but we know
        # they were added in chronological order.
        return incident.updated_at or incident.started_at
    return fallback_event.timestamp


def _create_incident_from_event(event: WebhookEvent) -> Incident:
    """Create a new incident seeded from a single event."""
    rule_name = ""
    parsed = event.parsed_data or {}
    if isinstance(parsed, dict):
        rule_name = str(
            parsed.get("RuleName") or parsed.get("AlertName") or parsed.get("alert_name") or ""
        )[:200]
    title = f"{event.source or 'unknown'} incident"
    if rule_name:
        title = f"{title} — {rule_name}"

    return Incident(
        title=title,
        status="active",
        source=event.source,
        started_at=event.timestamp or utcnow(),
        alert_count=1,
        top_importance=event.importance,
        member_ids=[event.id],
    )


def _add_event_to_incident(incident: Incident, event: WebhookEvent) -> None:
    """Add a single event to an existing active incident."""
    members: list[int] = list(incident.member_ids or [])
    if event.id in members:
        return
    if len(members) >= _MAX_MEMBERS_PER_INCIDENT:
        return
    members.append(event.id)
    incident.member_ids = members
    incident.alert_count = len(members)
    incident.updated_at = utcnow()
    # Keep the highest importance seen so far.
    if event.importance == "high" or incident.top_importance != "high":
        incident.top_importance = event.importance


async def _close_quiet_incidents(session: AsyncSession, now: Any) -> int:
    """Mark incidents as 'quiet' if their last activity was >= quiet_window ago.

    Returns the count of newly closed incidents.
    """
    quiet_threshold = now - timedelta(minutes=_INCIDENT_QUIET_MINUTES)
    result = await session.execute(
        select(Incident).where(
            Incident.status == "active",
            Incident.updated_at.isnot(None),
            Incident.updated_at <= quiet_threshold,
        )
    )
    quiet_list = list(result.scalars().all())

    closed = 0
    closed_ids: list[int] = []
    for incident in quiet_list:
        incident.status = "quiet"
        incident.ended_at = now
        incident.updated_at = now
        closed += 1
        closed_ids.append(incident.id)
        logger.info(
            "[Incidents] Incident quieted id=%s title=%s alerts=%d",
            incident.id,
            incident.title[:80],
            incident.alert_count,
        )

    if closed:
        await session.flush()

    # Generate LLM summaries for newly-closed incidents. Best-effort: a summary
    # failure doesn't re-open the incident. Runs inside the same session scope
    # so the summary is persisted atomically with the status change.
    if closed_ids:
        from services.incidents.summary import summarize_incident as _summarize

        for cid in closed_ids:
            try:
                await _summarize(session, cid)
            except Exception as e:
                logger.warning("[Incidents] Summary generation failed incident_id=%s error=%s", cid, e)

    return closed

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import SuppressedRecord


async def list_suppressed_records(
    session: AsyncSession,
    *,
    since_minutes: int = 60,
    limit: int = 100,
) -> list[dict[str, Any]]:
    since = datetime.now() - timedelta(minutes=max(1, since_minutes))
    stmt = (
        select(SuppressedRecord)
        .where(SuppressedRecord.created_at >= since)
        .order_by(SuppressedRecord.created_at.desc(), SuppressedRecord.id.desc())
        .limit(max(1, min(500, limit)))
    )
    rows = (await session.execute(stmt)).scalars().all()
    items: list[dict[str, Any]] = [
        {
            "id": r.id,
            "alert_hash": r.alert_hash,
            "source": r.source,
            "relation": r.relation,
            "root_cause_event_id": r.root_cause_event_id,
            "reason": r.reason,
            "related_alert_ids": list(r.related_alert_ids or []),
            "confidence": r.confidence,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]
    return items


async def count_suppressed_records(session: AsyncSession, *, since_minutes: int = 60) -> int | None:
    from db.session import count_with_timeout

    since = datetime.now() - timedelta(minutes=max(1, since_minutes))
    stmt = select(func.count()).select_from(SuppressedRecord).where(SuppressedRecord.created_at >= since)
    return await count_with_timeout(session, stmt)


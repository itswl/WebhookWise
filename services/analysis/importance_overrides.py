"""Operator importance corrections, applied to the next occurrence.

This is the loop that was missing. A correction used to change one alert row
and stop there; the same condition fired again and was judged from scratch.
Here a correction is stored against the condition's identity and applied to
every later firing of it — and says so, because an importance the model did
not produce must never look like one it did.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utcnow
from core.logger import get_logger
from models import ImportanceOverride

logger = get_logger("analysis.importance_overrides")

VALID_IMPORTANCE = ("high", "medium", "low")

# The marker the rest of the system reads to tell an operator decision from a
# model one. Absent means the model's own judgement stands.
OVERRIDE_KEY = "importance_override"


async def remember_override(
    session: AsyncSession,
    *,
    alert_hash: str,
    importance: str,
    source: str | None = None,
    alert_name: str | None = None,
    origin_event_id: int | None = None,
    actor: str = "dashboard",
) -> None:
    """Record a correction so the next occurrence inherits it.

    Upsert, not insert: correcting the same condition twice must replace the
    earlier decision rather than race it, so which one applies is never a
    question of insert order.
    """
    if not alert_hash or importance not in VALID_IMPORTANCE:
        return
    now = utcnow()
    statement = pg_insert(ImportanceOverride).values(
        alert_hash=alert_hash,
        importance=importance,
        source=(source or None),
        alert_name=alert_name[:200] if alert_name else None,
        origin_event_id=origin_event_id,
        actor=actor[:100],
        hit_count=0,
        created_at=now,
        updated_at=now,
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[ImportanceOverride.alert_hash],
            set_={
                "importance": importance,
                "source": (source or None),
                "alert_name": alert_name[:200] if alert_name else None,
                "origin_event_id": origin_event_id,
                "actor": actor[:100],
                "updated_at": now,
            },
        )
    )


async def apply_override(
    session: AsyncSession,
    *,
    alert_hash: str,
    analysis: dict[str, Any],
) -> dict[str, Any]:
    """Apply a stored correction to a fresh analysis result, visibly.

    Returns the analysis unchanged when there is nothing to apply. Never
    raises: a lookup failure must degrade to the model's own judgement rather
    than drop the alert.
    """
    if not alert_hash or not isinstance(analysis, dict):
        return analysis
    try:
        override = (
            await session.execute(select(ImportanceOverride).where(ImportanceOverride.alert_hash == alert_hash))
        ).scalar_one_or_none()
    except Exception as error:  # noqa: BLE001 — an override is an improvement, not a dependency
        logger.warning("Importance override lookup failed for %s: %s", alert_hash[:12], error)
        return analysis

    if override is None:
        return analysis

    model_importance = str(analysis.get("importance") or "")
    result = dict(analysis)
    result["importance"] = override.importance
    # Carried so the card, the dashboard and anyone reading the stored analysis
    # can tell this apart from the model's own call. A silent override is how
    # you end up arguing with a model that never said it.
    result[OVERRIDE_KEY] = {
        "applied": True,
        "importance": override.importance,
        "model_importance": model_importance,
        "actor": override.actor,
        "since": override.created_at.isoformat() if override.created_at else None,
    }

    now = utcnow()
    await session.execute(
        update(ImportanceOverride)
        .where(ImportanceOverride.id == override.id)
        .values(hit_count=ImportanceOverride.hit_count + 1, last_applied_at=now)
    )
    if model_importance and model_importance != override.importance:
        logger.info(
            "Importance override applied: %s -> %s (hash=%s, set by %s)",
            model_importance,
            override.importance,
            alert_hash[:12],
            override.actor,
        )
    return result


async def list_overrides(session: AsyncSession, limit: int = 200) -> list[dict[str, Any]]:
    rows = (
        (
            await session.execute(
                select(ImportanceOverride).order_by(ImportanceOverride.updated_at.desc()).limit(max(1, min(500, limit)))
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": row.id,
            "alert_hash": row.alert_hash,
            "importance": row.importance,
            "source": row.source,
            "alert_name": row.alert_name,
            "actor": row.actor,
            # An override nobody has hit since it was set is the one worth
            # questioning, so the count and the last hit are first-class.
            "hit_count": row.hit_count,
            "last_applied_at": row.last_applied_at.isoformat() if row.last_applied_at else None,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    ]


async def forget_override(session: AsyncSession, *, override_id: int) -> bool:
    override = await session.get(ImportanceOverride, override_id)
    if override is None:
        return False
    await session.delete(override)
    await session.commit()
    return True

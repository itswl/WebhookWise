"""Redis-first webhook deduplication helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from core.app_context import get_default_config
from core.logger import get_logger
from core.redis_client import redis_get_json_dict, redis_setex_json
from core.redis_keys import webhook_dedupe
from services.webhooks.types import AnalysisResult

logger = get_logger("webhooks.deduplication")


@dataclass(frozen=True, slots=True)
class CachedDuplicate:
    original_event_id: int
    analysis: AnalysisResult | None


def duplicate_window_hours() -> int:
    return int(get_default_config().retry.DUPLICATE_ALERT_TIME_WINDOW)


def _ttl_seconds() -> int:
    return max(60, duplicate_window_hours() * 3600)


async def get_cached_duplicate(alert_hash: str) -> CachedDuplicate | None:
    """Return duplicate metadata from Redis without touching PostgreSQL."""
    try:
        payload = await redis_get_json_dict(webhook_dedupe(alert_hash))
    except Exception as e:
        logger.warning("[Dedup] Redis duplicate lookup failed hash=%s error=%s", alert_hash[:12], e)
        return None

    if not payload:
        return None
    try:
        original_event_id = int(payload.get("original_event_id") or 0)
    except (TypeError, ValueError):
        return None
    if original_event_id <= 0:
        return None

    analysis = payload.get("analysis")
    return CachedDuplicate(
        original_event_id=original_event_id,
        analysis=cast(AnalysisResult, analysis) if isinstance(analysis, dict) else None,
    )


async def remember_duplicate_source(
    alert_hash: str,
    *,
    original_event_id: int,
    analysis: AnalysisResult | None,
) -> None:
    """Cache the canonical event for future duplicate checks."""
    if original_event_id <= 0:
        return
    payload: dict[str, Any] = {"original_event_id": original_event_id}
    if analysis:
        payload["analysis"] = analysis
    try:
        await redis_setex_json(webhook_dedupe(alert_hash), _ttl_seconds(), payload)
    except Exception as e:
        logger.warning("[Dedup] Redis duplicate cache write failed hash=%s error=%s", alert_hash[:12], e)

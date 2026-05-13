"""Redis-first webhook deduplication helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.logger import logger
from core.redis_client import redis_get_json_dict, redis_setex_json
from services.webhooks.policies import WebhookSavePolicy


@dataclass(frozen=True, slots=True)
class CachedDuplicate:
    original_event_id: int
    analysis: dict[str, Any] | None


def _dedupe_key(alert_hash: str) -> str:
    return f"webhook:dedupe:{alert_hash}"


def _ttl_seconds(policy: WebhookSavePolicy | None = None) -> int:
    save_policy = policy or WebhookSavePolicy.from_config()
    return max(60, int(save_policy.duplicate_window_hours) * 3600)


async def get_cached_duplicate(alert_hash: str) -> CachedDuplicate | None:
    """Return duplicate metadata from Redis without touching PostgreSQL."""
    from core.runtime_mode import is_lite_mode

    if is_lite_mode():
        return None
    try:
        payload = await redis_get_json_dict(_dedupe_key(alert_hash))
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
        analysis=analysis if isinstance(analysis, dict) else None,
    )


async def remember_duplicate_source(
    alert_hash: str,
    *,
    original_event_id: int,
    analysis: dict[str, Any] | None,
    policy: WebhookSavePolicy | None = None,
) -> None:
    """Cache the canonical event for future duplicate checks."""
    from core.runtime_mode import is_lite_mode

    if is_lite_mode():
        return
    if original_event_id <= 0:
        return
    payload: dict[str, Any] = {"original_event_id": original_event_id}
    if analysis:
        payload["analysis"] = analysis
    try:
        await redis_setex_json(_dedupe_key(alert_hash), _ttl_seconds(policy), payload)
    except Exception as e:
        logger.warning("[Dedup] Redis duplicate cache write failed hash=%s error=%s", alert_hash[:12], e)

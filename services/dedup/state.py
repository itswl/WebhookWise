import contextlib
from dataclasses import dataclass
from typing import Any

from core.redis_client import redis_get_json_dict, redis_setex_json
from core.redis_health import webhook_dedupe


@dataclass(frozen=True, slots=True)
class DedupState:
    dedup_key: str
    original_event_id: int
    first_seen_at: float
    last_seen_at: float
    count: int
    analysis: dict[str, Any] | None

    def is_active(self, now: float, window_seconds: int) -> bool:
        return (now - self.last_seen_at) <= window_seconds


def _dedup_state_key(dedup_key: str) -> str:
    return webhook_dedupe(dedup_key)


async def get_dedup_state(dedup_key: str) -> DedupState | None:
    try:
        payload = await redis_get_json_dict(_dedup_state_key(dedup_key))
    except Exception:
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
    return DedupState(
        dedup_key=dedup_key,
        original_event_id=original_event_id,
        first_seen_at=float(payload.get("first_seen_at") or 0),
        last_seen_at=float(payload.get("last_seen_at") or 0),
        count=int(payload.get("count") or 1),
        analysis=analysis if isinstance(analysis, dict) else None,
    )


async def remember_dedup_state(
    dedup_key: str,
    original_event_id: int,
    analysis: dict[str, Any] | None,
    ttl_seconds: int,
    *,
    now: float | None = None,
) -> None:
    import time

    current_time = now or time.time()
    existing = await get_dedup_state(dedup_key)
    count = (existing.count + 1) if existing else 1
    first_seen_at = existing.first_seen_at if existing else current_time

    payload: dict[str, Any] = {
        "dedup_key": dedup_key,
        "original_event_id": original_event_id,
        "first_seen_at": first_seen_at,
        "last_seen_at": current_time,
        "count": count,
    }
    if analysis:
        payload["analysis"] = analysis
    with contextlib.suppress(Exception):
        await redis_setex_json(_dedup_state_key(dedup_key), max(60, ttl_seconds), payload)

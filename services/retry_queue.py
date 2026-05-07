"""Redis-backed delayed retry queues.

The database remains the source of record for event/forward status, while Redis
holds the scheduling primitive that decides when a retry is due.
"""

from __future__ import annotations

import time
from typing import Any, cast

from core.redis_client import get_redis, redis_eval_str

_DRAIN_DUE_ZSET_LUA = """
local items = redis.call("zrangebyscore", KEYS[1], "-inf", ARGV[1], "LIMIT", 0, ARGV[2])
if #items == 0 then
  return ""
end
redis.call("zrem", KEYS[1], unpack(items))
return table.concat(items, ",")
"""

WEBHOOK_RETRY_ZSET = "webhook:retry:delayed"
FORWARD_RETRY_ZSET = "forward:retry:delayed"


def compute_backoff_delay(
    attempt: int,
    *,
    initial_delay: int,
    max_delay: int,
    multiplier: float,
) -> int:
    """Return bounded exponential backoff delay in seconds."""
    normalized_attempt = max(1, int(attempt))
    delay = initial_delay * (multiplier ** (normalized_attempt - 1))
    return max(0, int(min(delay, max_delay)))


async def _enqueue_due_id(zset_key: str, item_id: int, delay_seconds: int) -> None:
    score = time.time() + max(0, int(delay_seconds))
    redis = cast(Any, get_redis())
    await redis.zadd(zset_key, {str(item_id): score})


async def enqueue_webhook_retry(event_id: int, delay_seconds: int) -> None:
    await _enqueue_due_id(WEBHOOK_RETRY_ZSET, event_id, delay_seconds)


async def enqueue_forward_retry(failed_forward_id: int, delay_seconds: int) -> None:
    await _enqueue_due_id(FORWARD_RETRY_ZSET, failed_forward_id, delay_seconds)


async def drain_due_ids(zset_key: str, *, limit: int, now: float | None = None) -> list[int]:
    raw = await redis_eval_str(
        _DRAIN_DUE_ZSET_LUA, 1, zset_key, float(now if now is not None else time.time()), int(limit)
    )
    if not raw:
        return []
    return [int(part) for part in raw.split(",") if part.isdigit()]


async def drain_due_webhook_retries(*, limit: int) -> list[int]:
    return await drain_due_ids(WEBHOOK_RETRY_ZSET, limit=limit)


async def drain_due_forward_retries(*, limit: int) -> list[int]:
    return await drain_due_ids(FORWARD_RETRY_ZSET, limit=limit)

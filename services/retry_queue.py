"""Redis-backed delayed retry queue for failed forwarding.

Webhook processing retries use TaskIQ's dynamic scheduler directly. Failed
forwarding keeps this lightweight queue because retries are batched with
FailedForward audit records and per-batch concurrency controls.
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

FORWARD_RETRY_ZSET = "forward:retry:delayed"


async def _enqueue_due_id(zset_key: str, item_id: int, delay_seconds: int) -> None:
    score = time.time() + max(0, int(delay_seconds))
    redis = cast(Any, get_redis())
    await redis.zadd(zset_key, {str(item_id): score})


async def enqueue_forward_retry(failed_forward_id: int, delay_seconds: int) -> None:
    await _enqueue_due_id(FORWARD_RETRY_ZSET, failed_forward_id, delay_seconds)


async def drain_due_ids(zset_key: str, *, limit: int, now: float | None = None) -> list[int]:
    raw = await redis_eval_str(
        _DRAIN_DUE_ZSET_LUA, 1, zset_key, float(now if now is not None else time.time()), int(limit)
    )
    if not raw:
        return []
    return [int(part) for part in raw.split(",") if part.isdigit()]


async def drain_due_forward_retries(*, limit: int) -> list[int]:
    return await drain_due_ids(FORWARD_RETRY_ZSET, limit=limit)

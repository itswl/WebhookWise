from __future__ import annotations

from collections.abc import Awaitable
from typing import Any, cast

from core import redis_lifecycle
from core.redis_metrics import record_redis_operation
from core.redis_ops import coerce_int


async def redis_xlen(stream: str) -> int:
    r = redis_lifecycle.get_redis()
    raw = await record_redis_operation("xlen", r.xlen(stream))
    return coerce_int(raw)


async def redis_xpending_pending(stream: str, group: str) -> int:
    r = redis_lifecycle.get_redis()
    raw = await record_redis_operation(
        "xpending",
        cast(Awaitable[object], cast(Any, r).xpending(stream, group)),
    )
    if isinstance(raw, dict):
        try:
            return int(raw.get("pending") or 0)
        except (TypeError, ValueError):
            return 0
    if isinstance(raw, (list, tuple)) and raw:
        try:
            return int(raw[0] or 0)
        except (TypeError, ValueError, IndexError):
            return 0
    return 0


async def redis_xinfo_group_lag(stream: str, group: str) -> int:
    r = redis_lifecycle.get_redis()
    raw = await record_redis_operation(
        "xinfo_groups",
        cast(Awaitable[object], cast(Any, r).xinfo_groups(stream)),
    )
    if not isinstance(raw, list):
        return 0
    for item in raw:
        if not isinstance(item, dict):
            continue
        if str(item.get("name") or "") == group:
            try:
                return int(item.get("lag") or 0)
            except (TypeError, ValueError):
                return 0
    return 0

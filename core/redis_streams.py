from __future__ import annotations

from collections.abc import Awaitable
from typing import Any, cast

from core.redis_client import coerce_int, get_redis, record_redis_operation


async def redis_xlen(stream: str) -> int:
    raw = await record_redis_operation("xlen", get_redis().xlen(stream))
    return coerce_int(raw)


async def redis_xpending_pending(stream: str, group: str) -> int:
    raw = await record_redis_operation(
        "xpending",
        cast(Awaitable[object], cast(Any, get_redis()).xpending(stream, group)),
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
    raw = await record_redis_operation(
        "xinfo_groups",
        cast(Awaitable[object], cast(Any, get_redis()).xinfo_groups(stream)),
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

from __future__ import annotations

import time
from collections.abc import Awaitable
from typing import Any, cast

from core.redis_client import coerce_int, get_redis, record_redis_operation


async def redis_xlen(stream: str) -> int:
    raw = await record_redis_operation("xlen", get_redis().xlen(stream))
    return coerce_int(raw)


# Per-process depth cache for the ingress backpressure gate: the hot ingress
# path must not pay an XLEN round trip per request, and a slightly stale depth
# is fine for a high-water check. Under a burst, at most one XLEN per stream per
# TTL window is issued (a concurrent-refresh race just does a couple extra, both
# harmless). Returns None when never populated / on error, so callers fail open.
_XLEN_CACHE: dict[str, tuple[float, int]] = {}


async def redis_xlen_cached(stream: str, *, ttl_seconds: float = 2.0) -> int | None:
    now = time.monotonic()
    cached = _XLEN_CACHE.get(stream)
    if cached is not None and now < cached[0]:
        return cached[1]
    try:
        value = await redis_xlen(stream)
    except Exception:  # noqa: BLE001 - a depth probe failure must fail open, never block ingress
        return cached[1] if cached is not None else None
    _XLEN_CACHE[stream] = (now + ttl_seconds, value)
    return value


def _reset_xlen_cache_for_tests() -> None:
    _XLEN_CACHE.clear()


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

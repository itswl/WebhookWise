from __future__ import annotations

from collections.abc import Awaitable
from typing import Any, cast

from redis.asyncio.client import PubSub

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


class RedisPubSub:
    def __init__(self, inner: PubSub) -> None:
        self._inner = inner

    async def subscribe(self, channel: str) -> None:
        await self._inner.subscribe(channel)

    async def get_message(
        self, *, ignore_subscribe_messages: bool = True, timeout: float | None = None
    ) -> dict[str, Any] | None:
        timeout_value = float(timeout or 0.0)
        raw = await self._inner.get_message(
            ignore_subscribe_messages=ignore_subscribe_messages,
            timeout=timeout_value,
        )
        return raw if isinstance(raw, dict) else None

    async def close(self) -> None:
        await self._inner.close()


def redis_pubsub() -> RedisPubSub:
    return RedisPubSub(get_redis().pubsub())

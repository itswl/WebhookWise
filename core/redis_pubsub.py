from __future__ import annotations

from typing import Any

from redis.asyncio.client import PubSub

from core import redis_lifecycle


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
    return RedisPubSub(redis_lifecycle.get_redis().pubsub())

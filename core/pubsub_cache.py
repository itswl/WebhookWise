"""A tiny per-worker TTL cache with cross-worker Redis Pub/Sub invalidation.

Both forwarding rules and silences are read on every forward decision, cached
per worker for a short TTL, and invalidated across workers over a Redis Pub/Sub
channel when a write happens. That pattern was copy-pasted in
``services/forwarding/rules.py`` and ``services/silences/store.py`` (loader +
invalidate + publish + listener, ~identical). This collapses it into one
generic, testable helper; each module keeps a single instance and thin
module-level wrappers over it (so existing call sites and test patches on the
wrapper names keep working).

Semantics preserved exactly from the originals:
- monotonic-clock TTL (immune to wall-clock jumps);
- ``publish`` swallows Redis errors with a warning (a missed invalidate only
  means a stale read for up to one TTL — never a failed write);
- ``start_listener`` runs a background task that invalidates on any message and
  logs at debug; a dropped listener is warned, not fatal.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from core.logger import get_logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class TtlPubSubCache[T]:
    """Per-worker TTL cache of a value produced by ``loader``, invalidated across
    workers via a Redis Pub/Sub ``channel``.

    ``loader`` fetches the fresh value (typically a DB read returning a list of
    snapshots); it takes an optional ``AsyncSession`` so a caller inside a
    transaction can supply its own session for the reload. ``log_prefix`` is the
    bracketed tag used in log lines (e.g. "ForwardRules"). ``ttl_seconds``
    defaults to 30s to match the originals.
    """

    def __init__(
        self,
        *,
        channel: str,
        loader: Callable[[AsyncSession | None], Awaitable[T]],
        log_prefix: str,
        ttl_seconds: float = 30.0,
    ) -> None:
        self._channel = channel
        self._loader = loader
        self._log = get_logger(f"pubsub_cache.{log_prefix.lower()}")
        self._log_prefix = log_prefix
        self._ttl = ttl_seconds
        self._value: T | None = None
        self._loaded_at: float = 0.0

    def invalidate(self) -> None:
        """Drop the local cached value; the next get reloads it."""
        self._value = None
        self._loaded_at = 0.0

    async def get(self, session: AsyncSession | None = None) -> T:
        """Return the cached value, reloading via ``loader`` when stale/empty.

        The cache check is session-independent (a fresh value is returned as-is);
        ``session`` is only threaded to ``loader`` on a miss, so a hot-path caller
        that always passes its session still hits the cache instead of reloading.
        """
        now = time.monotonic()
        if self._value is not None and (now - self._loaded_at) < self._ttl:
            return self._value
        value = await self._loader(session)
        self._value = value
        self._loaded_at = now
        return value

    async def publish_invalidation(self) -> None:
        """Broadcast cache invalidation to all workers via Redis Pub/Sub.

        Best-effort: a publish failure only risks a stale read for up to one TTL,
        never a lost write, so it is logged and swallowed.
        """
        try:
            from core.redis_client import redis_publish

            await redis_publish(self._channel, "invalidate")
        except Exception as e:  # noqa: BLE001 - invalidation is best-effort, never a gate on the write
            self._log.warning("[%s] Failed to publish cache invalidation notification: %s", self._log_prefix, e)

    def start_listener(self) -> None:
        """Subscribe to the Pub/Sub channel for cross-worker invalidation.

        Call once per worker process at startup (e.g. in lifespan). Runs as a
        background task that invalidates the local cache when another worker
        publishes an update.
        """
        from redis.exceptions import RedisError

        from core.redis_client import get_redis

        async def _listen() -> None:
            client = get_redis()
            pubsub = client.pubsub()
            try:
                await pubsub.subscribe(self._channel)
                async for message in pubsub.listen():
                    if message.get("type") == "message":
                        self.invalidate()
                        self._log.debug(
                            "[%s] Received cross-process cache invalidation notification", self._log_prefix
                        )
            except (RedisError, OSError, RuntimeError) as e:
                self._log.warning("[%s] Pub/Sub listener interrupted: %s", self._log_prefix, e)
            finally:
                with contextlib.suppress(Exception):
                    await pubsub.unsubscribe(self._channel)
                    await pubsub.close()

        asyncio.create_task(_listen())

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
        self._invalidation_epoch = 0
        # Single-flight guard for reloads (see get()).
        self._reload_lock = asyncio.Lock()
        # Strong reference to the listener task: the event loop only keeps a
        # weak one, so an unreferenced listener could be garbage-collected and
        # silently stop delivering cross-worker invalidations.
        self._listener_task: asyncio.Task[None] | None = None

    def invalidate(self) -> None:
        """Drop the local cached value; the next get reloads it."""
        self._value = None
        self._loaded_at = 0.0
        # Epoch guard: an invalidation that lands while a loader is in flight
        # must not be clobbered by that (already-stale) load result committing
        # itself to the cache afterwards.
        self._invalidation_epoch += 1

    async def get(self, session: AsyncSession | None = None) -> T:
        """Return the cached value, reloading via ``loader`` when stale/empty.

        The cache check is session-independent (a fresh value is returned as-is);
        ``session`` is only threaded to ``loader`` on a miss, so a hot-path caller
        that always passes its session still hits the cache instead of reloading.
        """
        if self._value is not None and (time.monotonic() - self._loaded_at) < self._ttl:
            return self._value
        # Single-flight: when the TTL lapses under load, every in-flight forward
        # decision would otherwise call the loader concurrently (a stampede of
        # identical DB reads). Only the first waiter loads; the rest reuse it.
        async with self._reload_lock:
            if self._value is not None and (time.monotonic() - self._loaded_at) < self._ttl:
                return self._value
            epoch_before = self._invalidation_epoch
            value = await self._loader(session)
            # Commit to the cache only if no invalidation arrived while the
            # loader was running — otherwise this value may already be stale,
            # so return it to the caller but leave the cache empty for the
            # next get() to reload fresh.
            if self._invalidation_epoch == epoch_before:
                self._value = value
                self._loaded_at = time.monotonic()
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

    def invalidate_after_commit(self, session: AsyncSession) -> None:
        """Invalidate locally now and coalesce publication until commit."""
        from db.session import register_after_commit_action

        # Drop the local value immediately so this process does not keep serving
        # known-stale configuration. The action repeats invalidation after commit
        # in case a concurrent pre-commit miss reloaded the old database value.
        self.invalidate()

        async def _invalidate() -> None:
            self.invalidate()
            await self.publish_invalidation()

        register_after_commit_action(session, self._channel, _invalidate)

    def start_listener(self) -> None:
        """Subscribe to the Pub/Sub channel for cross-worker invalidation.

        Call once per worker process at startup (e.g. in lifespan). Runs as a
        background task that invalidates the local cache when another worker
        publishes an update.
        """
        if self._listener_task is not None and not self._listener_task.done():
            return

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
                        self._log.debug("[%s] Received cross-process cache invalidation notification", self._log_prefix)
            except (RedisError, OSError, RuntimeError) as e:
                self._log.warning("[%s] Pub/Sub listener interrupted: %s", self._log_prefix, e)
            finally:
                with contextlib.suppress(Exception):
                    await pubsub.unsubscribe(self._channel)
                    # redis-py 8 deprecates PubSub.close() in favour of aclose().
                    # types-redis stubs still describe redis 4.x, hence the ignore.
                    await pubsub.aclose()  # type: ignore[attr-defined]

        def _on_listener_done(task: asyncio.Task[None]) -> None:
            if self._listener_task is task:
                self._listener_task = None
            if not task.cancelled() and task.exception() is not None:
                self._log.warning(
                    "[%s] Pub/Sub listener task died: %s — cross-worker invalidation degraded to TTL expiry",
                    self._log_prefix,
                    task.exception(),
                )

        task = asyncio.create_task(_listen())
        task.add_done_callback(_on_listener_done)
        self._listener_task = task

    async def stop_listener(self) -> None:
        """Cancel and await the Pub/Sub listener if it is running."""
        task = self._listener_task
        if task is None:
            return
        self._listener_task = None
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

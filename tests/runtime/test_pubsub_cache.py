"""Unit tests for the shared TtlPubSubCache (extracted from rules/silences)."""

from __future__ import annotations

import asyncio

import pytest

from core.pubsub_cache import TtlPubSubCache


@pytest.mark.asyncio
async def test_caches_within_ttl_and_reloads_after(monkeypatch) -> None:
    calls: list[object] = []

    async def loader(session):
        calls.append(session)
        return f"value-{len(calls)}"

    clock = {"t": 1000.0}
    monkeypatch.setattr("core.pubsub_cache.time.monotonic", lambda: clock["t"])

    cache: TtlPubSubCache[str] = TtlPubSubCache(channel="ch", loader=loader, log_prefix="Test", ttl_seconds=30.0)

    assert await cache.get() == "value-1"  # miss → load
    assert await cache.get() == "value-1"  # within TTL → cached, no reload
    assert len(calls) == 1

    clock["t"] += 31.0  # TTL expired
    assert await cache.get() == "value-2"  # reload
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_invalidate_forces_reload(monkeypatch) -> None:
    calls: list[object] = []

    async def loader(session):
        calls.append(1)
        return len(calls)

    monkeypatch.setattr("core.pubsub_cache.time.monotonic", lambda: 5.0)  # frozen clock
    cache: TtlPubSubCache[int] = TtlPubSubCache(channel="ch", loader=loader, log_prefix="Test")

    assert await cache.get() == 1
    assert await cache.get() == 1  # cached (clock frozen, within TTL)
    cache.invalidate()
    assert await cache.get() == 2  # invalidate forced a reload despite fresh TTL


@pytest.mark.asyncio
async def test_session_threaded_to_loader_only_on_miss(monkeypatch) -> None:
    seen: list[object] = []

    async def loader(session):
        seen.append(session)
        return "v"

    clock = {"t": 0.0}
    monkeypatch.setattr("core.pubsub_cache.time.monotonic", lambda: clock["t"])
    cache: TtlPubSubCache[str] = TtlPubSubCache(channel="ch", loader=loader, log_prefix="Test")

    sentinel = object()
    await cache.get(sentinel)  # miss → loader gets the session
    await cache.get(sentinel)  # hit → loader NOT called again (cache is session-independent)
    assert seen == [sentinel]


@pytest.mark.asyncio
async def test_concurrent_misses_are_single_flight() -> None:
    calls: list[int] = []
    release = asyncio.Event()

    async def slow_loader(session):
        calls.append(1)
        await release.wait()
        return "loaded"

    cache: TtlPubSubCache[str] = TtlPubSubCache(channel="ch", loader=slow_loader, log_prefix="Test")

    # A burst of concurrent misses (TTL lapsed under load) must trigger exactly
    # one loader call; the rest wait for and reuse that result.
    tasks = [asyncio.create_task(cache.get()) for _ in range(5)]
    await asyncio.sleep(0)  # let all tasks reach the lock
    release.set()
    results = await asyncio.gather(*tasks)

    assert results == ["loaded"] * 5
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_start_listener_retains_task_reference(monkeypatch) -> None:
    started = asyncio.Event()

    class FakePubSub:
        async def subscribe(self, channel):
            started.set()

        def listen(self):
            async def _iter():
                # Stay alive until cancelled, like a real subscription.
                await asyncio.sleep(3600)
                yield {}

            return _iter()

        async def unsubscribe(self, channel):
            return None

        async def close(self):
            return None

    class FakeRedis:
        def pubsub(self):
            return FakePubSub()

    monkeypatch.setattr("core.redis_client.get_redis", lambda: FakeRedis())
    cache: TtlPubSubCache[int] = TtlPubSubCache(channel="ch", loader=lambda s: None, log_prefix="Test")

    cache.start_listener()
    await asyncio.wait_for(started.wait(), timeout=1)
    # The event loop holds only a weak reference to tasks; the cache must keep
    # a strong one so the listener can't be garbage-collected mid-flight.
    task = cache._listener_task
    assert task is not None and not task.done()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cache._listener_task is None  # done-callback cleared the slot


@pytest.mark.asyncio
async def test_publish_invalidation_swallows_redis_error(monkeypatch) -> None:
    async def boom(channel, message):
        raise RuntimeError("redis down")

    monkeypatch.setattr("core.redis_client.redis_publish", boom)
    cache: TtlPubSubCache[int] = TtlPubSubCache(channel="ch", loader=lambda s: None, log_prefix="Test")
    # Must not raise — a failed invalidation broadcast only risks a stale read.
    await cache.publish_invalidation()

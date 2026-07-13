"""Unit tests for the shared TtlPubSubCache (extracted from rules/silences)."""

from __future__ import annotations

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
async def test_publish_invalidation_swallows_redis_error(monkeypatch) -> None:
    async def boom(channel, message):
        raise RuntimeError("redis down")

    monkeypatch.setattr("core.redis_client.redis_publish", boom)
    cache: TtlPubSubCache[int] = TtlPubSubCache(channel="ch", loader=lambda s: None, log_prefix="Test")
    # Must not raise — a failed invalidation broadcast only risks a stale read.
    await cache.publish_invalidation()

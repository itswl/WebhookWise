"""Tests for global queue-depth ingress backpressure + queue-health readout."""

from __future__ import annotations

import pytest

from services.webhooks.policies import IngressPolicy


def _policy(**over: object) -> IngressPolicy:
    base: dict[str, object] = {
        "max_body_bytes": 0,
        "ingress_backpressure_threshold": 0,
        "ingress_backpressure_window_seconds": 1,
        "stream_maxlen": 1000,
        "ingress_high_water_fraction": 0.9,
        "mq_queue": "webhook:queue",
    }
    base.update(over)
    return IngressPolicy(**base)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _clear_depth_cache():
    from core.redis_streams import _reset_xlen_cache_for_tests

    _reset_xlen_cache_for_tests()
    yield
    _reset_xlen_cache_for_tests()


@pytest.mark.asyncio
async def test_rejects_when_depth_at_or_above_high_water(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.webhooks.ingress_backpressure as bp

    async def fake_depth(stream: str, *, ttl_seconds: float = 2.0) -> int:
        return 950  # ≥ 0.9 * 1000

    monkeypatch.setattr("core.redis_streams.redis_xlen_cached", fake_depth)
    result = await bp.check_queue_backpressure(policy=_policy())
    assert result.reject is True
    assert result.high_water == 900


@pytest.mark.asyncio
async def test_allows_below_high_water(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.webhooks.ingress_backpressure as bp

    async def fake_depth(stream: str, *, ttl_seconds: float = 2.0) -> int:
        return 500

    monkeypatch.setattr("core.redis_streams.redis_xlen_cached", fake_depth)
    result = await bp.check_queue_backpressure(policy=_policy())
    assert result.reject is False


@pytest.mark.asyncio
async def test_disabled_fraction_never_rejects(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.webhooks.ingress_backpressure as bp

    async def boom(stream: str, *, ttl_seconds: float = 2.0) -> int:
        raise AssertionError("depth must not be probed when the gate is disabled")

    monkeypatch.setattr("core.redis_streams.redis_xlen_cached", boom)
    # Default fraction 0.0 → gate off → no probe, no rejection.
    result = await bp.check_queue_backpressure(policy=_policy(ingress_high_water_fraction=0.0))
    assert result.reject is False


@pytest.mark.asyncio
async def test_fails_open_when_depth_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.webhooks.ingress_backpressure as bp

    async def unavailable(stream: str, *, ttl_seconds: float = 2.0) -> int | None:
        return None  # cache never populated / probe failed

    monkeypatch.setattr("core.redis_streams.redis_xlen_cached", unavailable)
    result = await bp.check_queue_backpressure(policy=_policy())
    assert result.reject is False  # fail open — a depth-probe gap must not block ingress


@pytest.mark.asyncio
async def test_xlen_cache_reuses_within_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    from core import redis_streams

    calls = {"n": 0}

    async def counting_xlen(stream: str) -> int:
        calls["n"] += 1
        return 42

    monkeypatch.setattr(redis_streams, "redis_xlen", counting_xlen)
    a = await redis_streams.redis_xlen_cached("webhook:queue", ttl_seconds=60)
    b = await redis_streams.redis_xlen_cached("webhook:queue", ttl_seconds=60)
    assert a == b == 42
    assert calls["n"] == 1  # second read served from cache, no extra XLEN


@pytest.mark.asyncio
async def test_queue_health_reports_fill_and_backlogged(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.operations import queue_health

    async def xlen(stream: str) -> int:
        return 850

    async def xpending(stream: str, group: str) -> int:
        return 40

    async def lag(stream: str, group: str) -> int:
        return 810

    monkeypatch.setattr(queue_health, "redis_xlen", xlen)
    monkeypatch.setattr(queue_health, "redis_xpending_pending", xpending)
    monkeypatch.setattr(queue_health, "redis_xinfo_group_lag", lag)

    cfg = queue_health.get_config_manager().mq
    monkeypatch.setattr(cfg, "WEBHOOK_MQ_STREAM_MAXLEN", 1000)
    monkeypatch.setattr(cfg, "WEBHOOK_MQ_BACKLOG_WARN_FRACTION", 0.8)

    health = await queue_health.get_queue_health()
    assert health["depth"] == 850
    assert health["pending"] == 40
    assert health["fill_fraction"] == 0.85
    assert health["backlogged"] is True  # 0.85 ≥ 0.8 warn threshold


@pytest.mark.asyncio
async def test_queue_health_degrades_when_probe_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    from redis.exceptions import RedisError

    from services.operations import queue_health

    async def boom(stream: str) -> int:
        raise RedisError("redis down")

    monkeypatch.setattr(queue_health, "redis_xlen", boom)
    health = await queue_health.get_queue_health()
    # Best-effort: unreadable metrics come back null, not an exception.
    assert health["depth"] is None
    assert health["fill_fraction"] is None
    assert health["backlogged"] is False

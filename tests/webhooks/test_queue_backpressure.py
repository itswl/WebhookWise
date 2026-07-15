"""Tests for global queue-backlog ingress backpressure + queue-health readout.

The risk metric is the UNCONSUMED backlog (undelivered lag + un-acked pending),
never total stream length — a busy stream's depth sits at MAXLEN of already-
acked entries, which is not a backlog.
"""

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
        "mq_consumer_group": "webhook-processors",
    }
    base.update(over)
    return IngressPolicy(**base)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _clear_backlog_cache():
    from core.redis_streams import _reset_backlog_cache_for_tests

    _reset_backlog_cache_for_tests()
    yield
    _reset_backlog_cache_for_tests()


@pytest.mark.asyncio
async def test_rejects_when_backlog_at_or_above_high_water(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.webhooks.ingress_backpressure as bp

    async def fake_backlog(stream: str, group: str, *, ttl_seconds: float = 2.0) -> int:
        return 950  # ≥ 0.9 * 1000

    monkeypatch.setattr("core.redis_streams.redis_group_backlog_cached", fake_backlog)
    result = await bp.check_queue_backpressure(policy=_policy())
    assert result.reject is True
    assert result.high_water == 900


@pytest.mark.asyncio
async def test_allows_below_high_water(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.webhooks.ingress_backpressure as bp

    async def fake_backlog(stream: str, group: str, *, ttl_seconds: float = 2.0) -> int:
        return 500

    monkeypatch.setattr("core.redis_streams.redis_group_backlog_cached", fake_backlog)
    result = await bp.check_queue_backpressure(policy=_policy())
    assert result.reject is False


@pytest.mark.asyncio
async def test_disabled_fraction_never_rejects(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.webhooks.ingress_backpressure as bp

    async def boom(stream: str, group: str, *, ttl_seconds: float = 2.0) -> int:
        raise AssertionError("backlog must not be probed when the gate is disabled")

    monkeypatch.setattr("core.redis_streams.redis_group_backlog_cached", boom)
    result = await bp.check_queue_backpressure(policy=_policy(ingress_high_water_fraction=0.0))
    assert result.reject is False


@pytest.mark.asyncio
async def test_fails_open_when_backlog_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.webhooks.ingress_backpressure as bp

    async def unavailable(stream: str, group: str, *, ttl_seconds: float = 2.0) -> int | None:
        return None  # cache never populated / probe failed

    monkeypatch.setattr("core.redis_streams.redis_group_backlog_cached", unavailable)
    result = await bp.check_queue_backpressure(policy=_policy())
    assert result.reject is False  # fail open — a probe gap must not block ingress


@pytest.mark.asyncio
async def test_backlog_cache_reuses_within_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    from core import redis_streams

    calls = {"n": 0}

    async def counting_pending(stream: str, group: str) -> int:
        calls["n"] += 1
        return 30

    async def zero_lag(stream: str, group: str) -> int:
        return 12

    monkeypatch.setattr(redis_streams, "redis_xpending_pending", counting_pending)
    monkeypatch.setattr(redis_streams, "redis_xinfo_group_lag", zero_lag)
    a = await redis_streams.redis_group_backlog_cached("webhook:queue", "g", ttl_seconds=60)
    b = await redis_streams.redis_group_backlog_cached("webhook:queue", "g", ttl_seconds=60)
    assert a == b == 42  # 30 pending + 12 lag
    assert calls["n"] == 1  # second read served from cache, no extra probe


@pytest.mark.asyncio
async def test_queue_health_backlogged_on_unconsumed_not_depth(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.operations import queue_health

    async def xlen(stream: str) -> int:
        return 850

    async def xpending(stream: str, group: str) -> int:
        return 800

    async def lag(stream: str, group: str) -> int:
        return 40

    monkeypatch.setattr(queue_health, "redis_xlen", xlen)
    monkeypatch.setattr(queue_health, "redis_xpending_pending", xpending)
    monkeypatch.setattr(queue_health, "redis_xinfo_group_lag", lag)
    cfg = queue_health.get_config_manager().mq
    monkeypatch.setattr(cfg, "WEBHOOK_MQ_STREAM_MAXLEN", 1000)
    monkeypatch.setattr(cfg, "WEBHOOK_MQ_BACKLOG_WARN_FRACTION", 0.8)

    health = await queue_health.get_queue_health()
    assert health["depth"] == 850
    assert health["backlog"] == 840  # 800 pending + 40 lag
    assert health["backlog_fraction"] == 0.84
    assert health["backlogged"] is True  # 0.84 ≥ 0.8


@pytest.mark.asyncio
async def test_queue_health_full_but_acked_stream_is_not_backlogged(monkeypatch: pytest.MonkeyPatch) -> None:
    """The production regression: depth at MAXLEN but everything acked → healthy."""
    from services.operations import queue_health

    async def xlen(stream: str) -> int:
        return 100002  # at/over MAXLEN

    async def zero(stream: str, group: str) -> int:
        return 0

    monkeypatch.setattr(queue_health, "redis_xlen", xlen)
    monkeypatch.setattr(queue_health, "redis_xpending_pending", zero)
    monkeypatch.setattr(queue_health, "redis_xinfo_group_lag", zero)
    cfg = queue_health.get_config_manager().mq
    monkeypatch.setattr(cfg, "WEBHOOK_MQ_STREAM_MAXLEN", 100000)
    monkeypatch.setattr(cfg, "WEBHOOK_MQ_BACKLOG_WARN_FRACTION", 0.8)

    health = await queue_health.get_queue_health()
    assert health["fill_fraction"] == round(100002 / 100000, 4)  # full (informational)
    assert health["backlog"] == 0
    assert health["backlogged"] is False  # acked entries are not a backlog


@pytest.mark.asyncio
async def test_queue_health_degrades_when_probe_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    from redis.exceptions import RedisError

    from services.operations import queue_health

    async def boom(stream: str) -> int:
        raise RedisError("redis down")

    monkeypatch.setattr(queue_health, "redis_xlen", boom)
    health = await queue_health.get_queue_health()
    assert health["depth"] is None
    assert health["backlog"] is None
    assert health["backlogged"] is False

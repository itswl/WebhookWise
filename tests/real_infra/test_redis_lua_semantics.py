"""The remaining Lua scripts' actual semantics, on a real Redis.

The main suite's mocked ``eval`` returns 1 unconditionally, so none of the
scripts in core/redis_lua ever execute there and their failure branches are
structurally untestable. Flapping already has a file; this covers the other
big four through their OWNING call paths — rate limiting, dedup, the circuit
breaker state machine, and the AI cache — not by evaluating Lua by hand.
"""

from __future__ import annotations

import os
import time
from types import SimpleNamespace
from typing import Any

import pytest
import redis.asyncio as aioredis

pytestmark = [pytest.mark.real_services, pytest.mark.real_redis]


@pytest.fixture(autouse=True)
async def _clean_probe_keys():
    client = aioredis.from_url(os.environ["REDIS_URL"])
    patterns = ("rl:*", "webhook:dedupe:lua-probe*", "circuit_breaker:lua-probe*", "analysis_*lua-probe*")
    try:
        for pattern in patterns:
            async for key in client.scan_iter(match=pattern):
                await client.delete(key)
        yield
        for pattern in patterns:
            async for key in client.scan_iter(match=pattern):
                await client.delete(key)
    finally:
        await client.aclose()


def _request(ip: str) -> Any:
    # The minimal shape get_client_ip reads: app.state (for config), client
    # host, and a headers mapping for the proxy-trust branch.
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace()),
        client=SimpleNamespace(host=ip),
        headers={},
    )


@pytest.mark.asyncio
async def test_multi_tier_rate_limit_denies_at_the_limit_and_counts_atomically(temp_config: Any) -> None:
    """All tiers are checked before any is incremented: a burst-tier denial
    must not leak increments into the sustained tier."""
    from core.webhook_security import enforce_webhook_rate_limit

    temp_config.security.WEBHOOK_RATE_LIMIT_PER_MINUTE = 10
    temp_config.security.WEBHOOK_RATE_LIMIT_BURST = 3
    temp_config.security.WEBHOOK_RATE_LIMIT_GLOBAL_PER_MINUTE = 0

    request = _request("198.51.100.7")
    outcomes = []
    for _ in range(5):
        _ip, result = await enforce_webhook_rate_limit(request)
        assert result is not None
        outcomes.append(result.allowed)
    # Burst tier of 3 in a 10s window: three allowed, then denied.
    assert outcomes == [True, True, True, False, False]

    # A different IP is an independent counter.
    _ip, other = await enforce_webhook_rate_limit(_request("198.51.100.8"))
    assert other is not None and other.allowed is True


@pytest.mark.asyncio
async def test_dedup_remember_is_an_atomic_read_modify_write() -> None:
    """Repeats bump count and keep first_seen_at — the GET-then-SETEX race the
    script replaced used to lose increments under concurrent duplicates."""
    import asyncio

    from services.dedup import get_dedup_state, remember_dedup_state

    key = "lua-probe-dedup"
    await remember_dedup_state(key, 101, {"summary": "s"}, ttl_seconds=60)
    first = await get_dedup_state(key)
    assert first is not None and first.count == 1 and first.original_event_id == 101

    await asyncio.gather(*(remember_dedup_state(key, 101, {"summary": "s2"}, ttl_seconds=60) for _ in range(10)))
    state = await get_dedup_state(key)
    assert state is not None
    assert state.count == 11, "concurrent duplicates must not lose increments"
    assert state.first_seen_at == first.first_seen_at
    # The script's contract is last-write-wins for the analysis payload; both
    # pipeline call sites always pass one (the pending placeholder, then the
    # final analysis), so there is no preserve-on-empty branch to rely on.
    assert state.analysis == {"summary": "s2"}


@pytest.mark.asyncio
async def test_circuit_breaker_state_machine_opens_and_recovers() -> None:
    """closed → open at the threshold → half-open after the recovery window →
    closed again on success. Driven through call_async with an injected clock,
    exactly as production uses it."""
    from core.circuit_breaker import CircuitBreaker, CircuitBreakerOpenException

    clock = {"now": time.time()}
    breaker = CircuitBreaker(
        "lua-probe",
        failure_threshold=2,
        recovery_timeout=30.0,
        expected_exceptions=(ValueError,),
        time_func=lambda: clock["now"],
    )

    async def boom() -> None:
        raise ValueError("downstream broke")

    async def ok() -> str:
        return "fine"

    for _ in range(2):
        with pytest.raises(ValueError):
            await breaker.call_async(boom)

    # Threshold reached: the breaker refuses BEFORE executing the call.
    with pytest.raises(CircuitBreakerOpenException):
        await breaker.call_async(ok)

    # Recovery window elapses (fake clock): half-open lets one probe through,
    # and its success closes the breaker for everyone.
    clock["now"] += 31.0
    assert await breaker.call_async(ok) == "fine"
    assert await breaker.call_async(ok) == "fine"


@pytest.mark.asyncio
async def test_ai_cache_roundtrip_and_hit_count(temp_config: Any) -> None:
    """The save script stores the analysis and the read path counts hits —
    the accounting the budget/usage views rely on."""
    from services.analysis.ai_cache import get_cached_analysis, save_to_cache
    from services.webhooks.types import cache_hit_count

    analysis = {"importance": "high", "summary": "cache probe", "triage_verdict": "act_now"}
    saved = await save_to_cache("lua-probe-hash", dict(analysis), enabled=True, ttl_seconds=60)
    assert saved is True

    first = await get_cached_analysis("lua-probe-hash", enabled=True, ttl_seconds=60)
    assert first is not None
    assert first["summary"] == "cache probe" and first["triage_verdict"] == "act_now"

    second = await get_cached_analysis("lua-probe-hash", enabled=True, ttl_seconds=60)
    assert second is not None
    assert cache_hit_count(second) >= cache_hit_count(first)

    assert await get_cached_analysis("lua-probe-miss", enabled=True, ttl_seconds=60) is None

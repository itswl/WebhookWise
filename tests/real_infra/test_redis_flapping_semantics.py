"""The flapping Lua script's actual semantics, on a real Redis."""

from __future__ import annotations

from datetime import timedelta

import pytest
import redis.asyncio as aioredis

from core.datetime_utils import utcnow
from services.webhooks.flapping import (
    ACTIVE_FLAPPING_KEY,
    FlappingPolicy,
    list_active_flapping,
    observe_flapping,
)

pytestmark = [pytest.mark.real_services, pytest.mark.real_redis]

_POLICY = FlappingPolicy(window_minutes=10, min_transitions=4, suppress_enabled=False)
_SOURCE = "real-redis-probe"


@pytest.fixture(autouse=True)
async def _clean_flap_keys():
    import os

    client = aioredis.from_url(os.environ["REDIS_URL"])
    try:
        async for key in client.scan_iter(match="flap:*"):
            await client.delete(key)
        yield
        async for key in client.scan_iter(match="flap:*"):
            await client.delete(key)
    finally:
        await client.aclose()


def _payload(status: str) -> dict[str, object]:
    return {"RuleName": "lua-probe", "status": status}


@pytest.mark.asyncio
async def test_flips_accumulate_and_cross_threshold() -> None:
    now = utcnow()
    statuses = ["firing", "resolved", "firing", "resolved", "firing"]
    flips_seen = []
    result = None
    for offset, status in enumerate(statuses):
        result = await observe_flapping(
            _SOURCE, _payload(status), None, policy=_POLICY, now=now + timedelta(seconds=offset)
        )
        flips_seen.append(result.flips)

    # First observation sets the baseline (0 flips); each alternation counts.
    assert flips_seen == [0, 1, 2, 3, 4]
    assert result is not None and result.flapping is True

    active = await list_active_flapping(now=now)
    assert [item["identity"] for item in active] == [f"{_SOURCE}::lua-probe"]


@pytest.mark.asyncio
async def test_same_status_repeats_do_not_count_as_flips() -> None:
    now = utcnow()
    for offset in range(5):
        result = await observe_flapping(
            _SOURCE, _payload("firing"), None, policy=_POLICY, now=now + timedelta(seconds=offset)
        )
    assert result.flips == 0
    assert result.flapping is False


@pytest.mark.asyncio
async def test_window_prunes_old_flips() -> None:
    now = utcnow()
    for offset, status in enumerate(["firing", "resolved", "firing", "resolved"]):
        await observe_flapping(_SOURCE, _payload(status), None, policy=_POLICY, now=now + timedelta(seconds=offset))

    # Beyond the window the flip zset is pruned: one fresh flip stands alone.
    later = now + timedelta(minutes=_POLICY.window_minutes + 1)
    result = await observe_flapping(_SOURCE, _payload("firing"), None, policy=_POLICY, now=later)
    assert result.flips <= 1
    assert result.flapping is False


@pytest.mark.asyncio
async def test_active_listing_expires_by_score() -> None:
    now = utcnow()
    for offset, status in enumerate(["firing", "resolved", "firing", "resolved", "firing"]):
        await observe_flapping(_SOURCE, _payload(status), None, policy=_POLICY, now=now + timedelta(seconds=offset))
    assert await list_active_flapping(now=now) != []

    beyond = now + timedelta(minutes=_POLICY.window_minutes + 2)
    assert await list_active_flapping(now=beyond) == []
    # The prune is persisted: the zset member is actually gone.
    client = aioredis.from_url(__import__("os").environ["REDIS_URL"])
    try:
        assert await client.zcard(ACTIVE_FLAPPING_KEY) == 0
    finally:
        await client.aclose()

"""Flapping detection semantics, fail-open behavior, and decision wiring."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from redis.exceptions import RedisError

from services.webhooks import flapping as flap_mod
from services.webhooks.decisioning import ForwardingPolicy, decide_forwarding
from services.webhooks.flapping import (
    FlappingPolicy,
    flap_identity,
    list_active_flapping,
    observe_flapping,
)


def test_flap_identity_is_source_plus_rule_and_status_insensitive() -> None:
    firing = {"RuleName": "gpu-mem-high", "status": "firing"}
    recovered = {"RuleName": "gpu-mem-high", "status": "resolved"}
    assert flap_identity("volcengine", firing) == "volcengine::gpu-mem-high"
    assert flap_identity("volcengine", firing) == flap_identity("volcengine", recovered)
    assert flap_identity("", None) == "unknown::unknown"


@pytest.mark.asyncio
async def test_observe_reports_flapping_at_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    async def eval_returning(*args: Any) -> int:
        return 6

    monkeypatch.setattr(flap_mod, "redis_eval_int", eval_returning)
    policy = FlappingPolicy(window_minutes=10, min_transitions=6, suppress_enabled=False)
    status = await observe_flapping("volcengine", {"RuleName": "gpu"}, None, policy=policy)
    assert status.flapping is True
    assert status.flips == 6


@pytest.mark.asyncio
async def test_observe_below_threshold_and_recovery_status(monkeypatch: pytest.MonkeyPatch) -> None:
    seen_status: list[str] = []

    async def eval_capturing(script: str, numkeys: int, *args: Any) -> int:
        seen_status.append(str(args[4]))  # ARGV[2] == observed status
        return 2

    monkeypatch.setattr(flap_mod, "redis_eval_int", eval_capturing)
    policy = FlappingPolicy(min_transitions=6)
    status = await observe_flapping("volcengine", {"RuleName": "gpu", "status": "恢复"}, None, policy=policy)
    assert status.flapping is False
    assert seen_status == ["recovery"]


@pytest.mark.asyncio
async def test_observe_fails_open_on_redis_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def eval_boom(*args: Any) -> int:
        raise RedisError("redis down")

    monkeypatch.setattr(flap_mod, "redis_eval_int", eval_boom)
    status = await observe_flapping("volcengine", {"RuleName": "gpu"}, None, policy=FlappingPolicy())
    assert status.flapping is False
    assert status.flips == 0


@pytest.mark.asyncio
async def test_list_active_flapping_reads_and_prunes(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock()
    client.zremrangebyscore = AsyncMock(return_value=1)
    client.zrevrange = AsyncMock(return_value=[(b"volcengine::gpu", 1789000000000.0)])
    monkeypatch.setattr(flap_mod, "get_redis", lambda: client)
    items = await list_active_flapping(limit=5)
    assert items == [{"identity": "volcengine::gpu", "quiet_at_ms": 1789000000000}]
    client.zremrangebyscore.assert_awaited_once()


@pytest.mark.asyncio
async def test_list_active_flapping_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    def get_boom() -> Any:
        raise RedisError("no redis")

    monkeypatch.setattr(flap_mod, "get_redis", get_boom)
    assert await list_active_flapping() == []


def _policy() -> ForwardingPolicy:
    return ForwardingPolicy(
        enable_periodic_reminder=False,
        reminder_interval_hours=6,
        notification_cooldown_seconds=60,
    )


def test_decide_forwarding_skips_flapping_with_trace_code() -> None:
    decision = decide_forwarding(rules=[], policy=_policy(), source="volcengine", flapping=True)
    assert decision.should_forward is False
    assert decision.skip_code == "flapping"


def test_decide_forwarding_without_flap_flag_unchanged() -> None:
    decision = decide_forwarding(rules=[], policy=_policy(), source="volcengine", flapping=False)
    assert decision.skip_code == "no_match"

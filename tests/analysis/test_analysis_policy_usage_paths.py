from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import pytest


def test_openclaw_poll_policy_methods_are_bounded() -> None:
    from core.datetime_utils import utcnow
    from services.analysis.openclaw_client import OpenClawPollPolicy, _describe_exception

    policy = OpenClawPollPolicy(
        timeout_seconds=120,
        poll_timeout_seconds=10,
        poll_initial_delay_seconds=2,
        poll_max_delay_seconds=20,
        poll_backoff_multiplier=2.0,
        http_api_url="https://openclaw.example",
        gateway_url="wss://gateway.example",
        gateway_token="gateway",
        hooks_token="hooks",
        connect_timeout_seconds=30,
        stability_required_hits=2,
        stability_ttl_seconds=60,
        max_consecutive_errors=3,
        enable_degradation=True,
        notification_webhook_url="https://example.com/notify",
    )

    assert policy.has_http_api is True
    assert policy.http_poll_timeout == 10.0
    assert policy.http_connect_timeout == 10.0
    assert policy.poll_claim_lease_seconds == 120
    assert policy.clamp_delay_to_timeout(15, None) == 15
    remaining_delay = policy.clamp_delay_to_timeout(30, utcnow() - timedelta(seconds=115))
    assert 1 <= remaining_delay <= 6
    assert policy.clamp_delay_to_timeout(30, utcnow() - timedelta(seconds=130)) == 1
    assert policy.delay_for_attempt(0) == 2
    assert policy.delay_for_attempt(3) == 16
    assert policy.delay_for_attempt(10) == 20
    assert policy.http_auth_headers("trace-1") == {"Authorization": "Bearer hooks", "X-Trace-Id": "trace-1"}
    assert _describe_exception(RuntimeError()) == "RuntimeError()"
    assert _describe_exception(RuntimeError("boom")) == "boom"


@pytest.mark.asyncio
async def test_ai_usage_endpoint_uses_minute_cache_and_writes_best_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.v1 import ai_usage

    cache: dict[str, dict[str, object]] = {}
    stats_calls: list[str] = []

    async def redis_get_json_dict(key: str) -> dict[str, object] | None:
        return cache.get(key)

    async def redis_setex_json(key: str, ttl: int, payload: dict[str, object]) -> None:
        assert ttl == 70
        cache[key] = payload

    async def get_ai_usage_stats(_session: object, period: str) -> dict[str, object]:
        stats_calls.append(period)
        return {"period": period, "total_cost": 1.25}

    monkeypatch.setattr("core.redis_client.redis_get_json_dict", redis_get_json_dict)
    monkeypatch.setattr("core.redis_client.redis_setex_json", redis_setex_json)
    monkeypatch.setattr(ai_usage, "get_ai_usage_stats", get_ai_usage_stats)
    monkeypatch.setattr(ai_usage.time, "time", lambda: 120.0)

    first = await ai_usage.get_ai_usage_endpoint(period="day", session=object())  # type: ignore[arg-type]
    second = await ai_usage.get_ai_usage_endpoint(period="day", session=object())  # type: ignore[arg-type]

    assert first == {"success": True, "data": {"period": "day", "total_cost": 1.25}}
    assert second == first
    assert stats_calls == ["day"]


@pytest.mark.asyncio
async def test_log_ai_usage_records_cost_and_suppresses_db_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.analysis import ai_usage

    added: list[object] = []

    class Policy:
        model = "gpt-test"

        def cost_for_tokens(self, tokens_in: int, tokens_out: int) -> float:
            return (tokens_in + tokens_out) / 1000

    class Session:
        def add_all(self, items: list[object]) -> None:
            added.extend(items)

    @asynccontextmanager
    async def session_scope() -> Any:
        yield Session()

    monkeypatch.setattr(ai_usage, "session_scope", session_scope)

    await ai_usage.log_ai_usage(
        "ai",
        alert_hash="hash-1",
        source="prometheus",
        tokens_in=100,
        tokens_out=50,
        policy=Policy(),  # type: ignore[arg-type]
    )
    # Rows are buffered; the flush is what performs the batched write.
    await ai_usage.flush_ai_usage()

    assert len(added) == 1
    assert added[0].model == "gpt-test"
    assert added[0].tokens_in == 100
    assert added[0].tokens_out == 50
    assert added[0].cost_estimate == 0.15

    @asynccontextmanager
    async def failing_session_scope() -> Any:
        raise RuntimeError("db down")
        yield

    monkeypatch.setattr(ai_usage, "session_scope", failing_session_scope)

    await ai_usage.log_ai_usage("cache", alert_hash="hash-2", source="grafana", policy=Policy())  # type: ignore[arg-type]
    # The batched write must swallow DB errors (usage rows are best-effort).
    await ai_usage.flush_ai_usage()


@pytest.mark.asyncio
async def test_log_ai_usage_flushes_immediately_when_buffer_is_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.analysis import ai_usage

    written_batches: list[list[object]] = []

    class Session:
        def add_all(self, items: list[object]) -> None:
            written_batches.append(list(items))

    @asynccontextmanager
    async def session_scope() -> Any:
        yield Session()

    class Policy:
        model = "gpt-test"

        def cost_for_tokens(self, tokens_in: int, tokens_out: int) -> float:
            return 0.0

    monkeypatch.setattr(ai_usage, "session_scope", session_scope)
    monkeypatch.setattr(ai_usage, "_BUFFER_MAX", 3)

    for index in range(3):
        await ai_usage.log_ai_usage("cache", alert_hash=f"h-{index}", source="grafana", policy=Policy())  # type: ignore[arg-type]

    # Hitting the buffer cap writes one batched INSERT without waiting for the timer.
    assert len(written_batches) == 1
    assert len(written_batches[0]) == 3
    # Nothing left behind for the delayed flush.
    await ai_usage.flush_ai_usage()
    assert len(written_batches) == 1


@pytest.mark.asyncio
async def test_log_ai_usage_schedules_one_delayed_flush_for_partial_buffer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.analysis import ai_usage

    class Policy:
        model = "gpt-test"

        def cost_for_tokens(self, tokens_in: int, tokens_out: int) -> float:
            return 0.0

    await ai_usage.log_ai_usage("cache", alert_hash="h-1", source="grafana", policy=Policy())  # type: ignore[arg-type]
    first_timer = ai_usage._flush_timer
    assert first_timer is not None

    await ai_usage.log_ai_usage("cache", alert_hash="h-2", source="grafana", policy=Policy())  # type: ignore[arg-type]
    # A second row within the window reuses the pending timer instead of stacking timers.
    assert ai_usage._flush_timer is first_timer

    # flush_ai_usage cancels the pending timer and drains the buffer.
    written: list[int] = []

    class Session:
        def add_all(self, items: list[object]) -> None:
            written.append(len(items))

    @asynccontextmanager
    async def session_scope() -> Any:
        yield Session()

    monkeypatch.setattr(ai_usage, "session_scope", session_scope)
    await ai_usage.flush_ai_usage()
    assert written == [2]
    assert ai_usage._flush_timer is None

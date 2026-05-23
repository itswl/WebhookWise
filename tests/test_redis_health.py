import pytest


@pytest.mark.asyncio
async def test_record_redis_operation_updates_shared_health_state() -> None:
    from core.redis_client import record_redis_operation
    from core.redis_health import RedisHealthState, get_redis_health_snapshot

    async def ok() -> str:
        return "pong"

    async def fail() -> str:
        raise RuntimeError("redis unavailable")

    assert await record_redis_operation("ping", ok()) == "pong"
    assert get_redis_health_snapshot().state == RedisHealthState.HEALTHY

    with pytest.raises(RuntimeError):
        await record_redis_operation("get", fail())

    snapshot = get_redis_health_snapshot()
    assert snapshot.state == RedisHealthState.UNAVAILABLE
    assert snapshot.consecutive_failures == 1
    assert snapshot.last_operation == "get"


@pytest.mark.asyncio
async def test_unavailable_health_state_throttles_recovery_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.redis_health import ensure_redis_available, mark_redis_failure

    class RedisClient:
        async def ping(self) -> bool:
            raise AssertionError("probe should be throttled")

    mark_redis_failure("eval", RuntimeError("redis unavailable"))
    monkeypatch.setattr("core.redis_client.get_redis", lambda: RedisClient())

    assert await ensure_redis_available("test", probe_interval=60) is False

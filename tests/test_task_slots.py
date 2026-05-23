import pytest


@pytest.mark.asyncio
async def test_task_slot_manager_uses_lua_registry_scripts() -> None:
    from core.redis_lua import TASK_SLOT_ACQUIRE, TASK_SLOT_RELEASE

    assert TASK_SLOT_ACQUIRE
    assert TASK_SLOT_RELEASE
    assert "zcard" in TASK_SLOT_ACQUIRE.lower()
    assert "zrem" in TASK_SLOT_RELEASE.lower()


@pytest.mark.asyncio
async def test_webhook_task_slot_uses_redis_global_slot(monkeypatch: pytest.MonkeyPatch, temp_config) -> None:
    from services.operations import tasks

    monkeypatch.setattr(temp_config.tasks, "MAX_CONCURRENT_WEBHOOK_TASKS", 2)
    monkeypatch.setattr(temp_config.tasks, "WEBHOOK_TASK_SLOT_LEASE_SECONDS", 30)

    calls: list[str] = []

    async def fake_eval(script: str, numkeys: int, *args: object) -> int:
        calls.append(script)
        return 1

    monkeypatch.setattr("services.operations.policies.redis_eval_int", fake_eval)

    async with tasks._webhook_task_slot():
        assert len(calls) >= 1
        assert "zcard" in calls[0].lower()

    assert len(calls) >= 2
    assert "zrem" in calls[-1].lower()


@pytest.mark.asyncio
async def test_webhook_task_slot_waits_for_redis_global_slot_recovery(
    monkeypatch: pytest.MonkeyPatch, temp_config
) -> None:
    import asyncio

    from services.operations import tasks

    monkeypatch.setattr(temp_config.tasks, "MAX_CONCURRENT_WEBHOOK_TASKS", 1)
    monkeypatch.setattr(temp_config.retry, "PROCESSING_LOCK_POLL_INTERVAL_MS", 50)
    monkeypatch.setattr(tasks, "_webhook_task_semaphore", None)
    monkeypatch.setattr(tasks, "_webhook_task_semaphore_limit", 0)

    entered = asyncio.Event()

    async def enter_slot() -> None:
        async with tasks._webhook_task_slot():
            entered.set()

    task = asyncio.create_task(enter_slot())
    await asyncio.wait_for(entered.wait(), timeout=0.5)
    await task

    assert entered.is_set() is True


@pytest.mark.asyncio
async def test_scheduled_task_leader_skips_when_redis_is_unavailable() -> None:
    from core.redis_health import mark_redis_failure
    from services.operations import tasks

    mark_redis_failure("test", RuntimeError("redis unavailable"))

    async with tasks._scheduled_task_leader("maintenance", 60) as is_leader:
        assert is_leader is False


def test_background_scan_interval_has_minimum_floor(monkeypatch: pytest.MonkeyPatch, temp_config) -> None:
    from services.operations import tasks

    monkeypatch.setattr(temp_config.tasks, "BACKGROUND_SCAN_INTERVAL_SECONDS", 10)

    assert tasks._background_scan_interval_seconds() == 30


def test_redis_stream_broker_has_pending_reclaim_timeout() -> None:
    from typing import Any, cast

    from core.taskiq_broker import broker

    if not hasattr(broker, "unacknowledged_lock_timeout"):
        pytest.skip("In-memory broker does not expose Redis Stream pending reclaim settings")

    redis_broker = cast(Any, broker)
    assert redis_broker.unacknowledged_lock_timeout is not None
    assert redis_broker.idle_timeout > 0

import pytest


@pytest.mark.asyncio
async def test_task_slot_manager_uses_lua_registry_scripts() -> None:
    import logging

    from core.redis_lua import TASK_SLOT_ACQUIRE, TASK_SLOT_RELEASE
    from services.operations.task_slots import TaskSlotManager

    calls: list[str] = []

    async def fake_eval(script: str, numkeys: int, *args: object) -> int:
        calls.append(script)
        return 1

    manager = TaskSlotManager(key="slot-key", eval_int=fake_eval, logger=logging.getLogger("test"))

    assert await manager.acquire("token", limit=2, lease_seconds=30) is True
    await manager.release("token")

    assert calls == [TASK_SLOT_ACQUIRE, TASK_SLOT_RELEASE]


@pytest.mark.asyncio
async def test_webhook_task_slot_uses_redis_global_slot(monkeypatch: pytest.MonkeyPatch, temp_config) -> None:
    from core.redis_lua import TASK_SLOT_ACQUIRE, TASK_SLOT_RELEASE
    from services.operations import tasks

    monkeypatch.setattr(temp_config.tasks, "MAX_CONCURRENT_WEBHOOK_TASKS", 2)
    monkeypatch.setattr(temp_config.tasks, "WEBHOOK_TASK_SLOT_LEASE_SECONDS", 30)

    calls: list[str] = []

    async def fake_eval(script: str, numkeys: int, *args: object) -> int:
        calls.append(script)
        return 1

    monkeypatch.setattr(tasks, "_redis_eval_int", fake_eval)

    async with tasks._webhook_task_slot():
        assert calls == [TASK_SLOT_ACQUIRE]

    assert calls == [TASK_SLOT_ACQUIRE, TASK_SLOT_RELEASE]


@pytest.mark.asyncio
async def test_webhook_task_slot_falls_back_to_local_limit_on_redis_error(
    monkeypatch: pytest.MonkeyPatch, temp_config
) -> None:
    from services.operations import tasks

    monkeypatch.setattr(temp_config.tasks, "MAX_CONCURRENT_WEBHOOK_TASKS", 1)
    monkeypatch.setattr(tasks, "_webhook_task_semaphore", None)
    monkeypatch.setattr(tasks, "_webhook_task_semaphore_limit", 0)

    async def failing_eval(script: str, numkeys: int, *args: object) -> int:
        raise RuntimeError("redis unavailable")

    monkeypatch.setattr(tasks, "_redis_eval_int", failing_eval)

    async with tasks._webhook_task_slot():
        assert tasks._webhook_task_semaphore is not None


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

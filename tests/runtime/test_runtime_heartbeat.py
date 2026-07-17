from __future__ import annotations

import asyncio

import pytest


def test_runtime_heartbeat_key_is_role_and_host_scoped() -> None:
    from core.runtime_heartbeat import runtime_heartbeat_key

    assert runtime_heartbeat_key("WORKER", hostname="Pod-1") == "webhookwise:runtime-heartbeat:worker:pod-1"
    assert runtime_heartbeat_key("scheduler", hostname="pod-1").endswith(":scheduler:pod-1")
    with pytest.raises(ValueError, match="Unsupported"):
        runtime_heartbeat_key("api", hostname="pod-1")


@pytest.mark.asyncio
async def test_runtime_heartbeat_start_is_idempotent_and_stop_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core import runtime_heartbeat

    writes: list[str] = []
    deletes: list[str] = []

    async def write(role: str) -> None:
        writes.append(role)

    async def delete(key: str) -> int:
        deletes.append(key)
        return 1

    monkeypatch.setattr(runtime_heartbeat, "_write_heartbeat", write)
    monkeypatch.setattr(runtime_heartbeat, "redis_delete", delete)
    monkeypatch.setattr(runtime_heartbeat, "heartbeat_interval_seconds", lambda: 3600)

    await runtime_heartbeat.start_runtime_heartbeat("worker")
    first_task = runtime_heartbeat._tasks["worker"]
    await runtime_heartbeat.start_runtime_heartbeat("worker")

    assert runtime_heartbeat._tasks["worker"] is first_task
    assert writes == ["worker"]

    await runtime_heartbeat.stop_runtime_heartbeat("worker")
    assert first_task.cancelled()
    assert deletes == [runtime_heartbeat.runtime_heartbeat_key("worker")]


@pytest.mark.asyncio
async def test_runtime_heartbeat_freshness_rejects_missing_stale_and_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core import runtime_heartbeat

    values = iter([None, "invalid", "900", "995"])

    async def get_value(_key: str) -> str | None:
        return next(values)

    monkeypatch.setattr(runtime_heartbeat, "redis_get_str", get_value)
    monkeypatch.setattr(runtime_heartbeat.time, "time", lambda: 1000.0)
    monkeypatch.setattr(runtime_heartbeat, "heartbeat_interval_seconds", lambda: 10)
    monkeypatch.setattr(runtime_heartbeat, "heartbeat_ttl_seconds", lambda: 45)

    assert await runtime_heartbeat.runtime_heartbeat_is_fresh("scheduler") is False
    assert await runtime_heartbeat.runtime_heartbeat_is_fresh("scheduler") is False
    assert await runtime_heartbeat.runtime_heartbeat_is_fresh("scheduler") is False
    assert await runtime_heartbeat.runtime_heartbeat_is_fresh("scheduler") is True

    # Ensure no task from another test leaks into this event loop.
    await asyncio.sleep(0)

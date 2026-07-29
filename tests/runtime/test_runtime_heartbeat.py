from __future__ import annotations

import asyncio
from pathlib import Path

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


@pytest.mark.asyncio
async def test_write_heartbeat_touches_local_file_even_when_redis_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core import runtime_heartbeat

    heartbeat_file = tmp_path / "heartbeat"
    monkeypatch.setenv("WEBHOOK_LOCAL_HEARTBEAT_FILE", str(heartbeat_file))

    async def failing_setex(_key: str, _ttl: int, _value: str) -> bool:
        raise RuntimeError("redis down")

    monkeypatch.setattr(runtime_heartbeat, "redis_setex_str", failing_setex)

    with pytest.raises(RuntimeError, match="redis down"):
        await runtime_heartbeat._write_heartbeat("worker")

    # The local liveness signal must refresh even while Redis is unreachable.
    assert float(heartbeat_file.read_text(encoding="utf-8")) > 0


@pytest.mark.asyncio
async def test_write_heartbeat_survives_unwritable_local_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from core import runtime_heartbeat

    monkeypatch.setenv("WEBHOOK_LOCAL_HEARTBEAT_FILE", str(tmp_path / "missing-dir" / "heartbeat"))
    written_keys: list[str] = []

    async def record_setex(key: str, _ttl: int, _value: str) -> bool:
        written_keys.append(key)
        return True

    monkeypatch.setattr(runtime_heartbeat, "redis_setex_str", record_setex)

    await runtime_heartbeat._write_heartbeat("worker")

    assert written_keys == [runtime_heartbeat.runtime_heartbeat_key("worker")]


def test_local_heartbeat_file_path_defaults_and_strips_blank_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from core import runtime_heartbeat

    monkeypatch.delenv("WEBHOOK_LOCAL_HEARTBEAT_FILE", raising=False)
    assert runtime_heartbeat.local_heartbeat_file_path() == runtime_heartbeat.DEFAULT_LOCAL_HEARTBEAT_FILE

    monkeypatch.setenv("WEBHOOK_LOCAL_HEARTBEAT_FILE", "  ")
    assert runtime_heartbeat.local_heartbeat_file_path() == runtime_heartbeat.DEFAULT_LOCAL_HEARTBEAT_FILE

    monkeypatch.setenv("WEBHOOK_LOCAL_HEARTBEAT_FILE", "/data/hb")
    assert runtime_heartbeat.local_heartbeat_file_path() == "/data/hb"

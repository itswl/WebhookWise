from unittest.mock import AsyncMock

import pytest

from services.operations.taskiq_retry_scheduler import compute_backoff_delay


def test_compute_backoff_delay_is_bounded() -> None:
    assert compute_backoff_delay(1, initial_delay=30, max_delay=900, multiplier=2.0) == 30
    assert compute_backoff_delay(3, initial_delay=30, max_delay=900, multiplier=2.0) == 120
    assert compute_backoff_delay(99, initial_delay=30, max_delay=900, multiplier=2.0) == 900


@pytest.mark.asyncio
async def test_schedule_webhook_retry_uses_taskiq_dynamic_schedule(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.operations.taskiq_retry_scheduler as scheduler
    import services.operations.tasks as tasks

    source = AsyncMock()
    captured: dict[str, object] = {}

    class FakeKicker:
        def with_schedule_id(self, schedule_id: str) -> "FakeKicker":
            captured["schedule_id"] = schedule_id
            return self

        async def schedule_by_time(self, schedule_source: object, run_at: object, **kwargs: object) -> None:
            captured["schedule_source"] = schedule_source
            captured["run_at"] = run_at
            captured["kwargs"] = kwargs

    class FakeTask:
        def kicker(self) -> FakeKicker:
            return FakeKicker()

    monkeypatch.setattr(scheduler, "dynamic_schedule_source", source)
    monkeypatch.setattr(tasks, "process_webhook_task", FakeTask())

    await scheduler.schedule_webhook_retry(123, 30)

    source.delete_schedule.assert_awaited_once_with("webhook-retry:123")
    assert captured["schedule_id"] == "webhook-retry:123"
    assert captured["schedule_source"] is source
    assert captured["kwargs"] == {"event_id": 123, "client_ip": "retry-schedule"}


@pytest.mark.asyncio
async def test_schedule_forward_retry_uses_taskiq_dynamic_schedule(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.operations.taskiq_retry_scheduler as scheduler
    import services.operations.tasks as tasks

    source = AsyncMock()
    captured: dict[str, object] = {}

    class FakeKicker:
        def with_schedule_id(self, schedule_id: str) -> "FakeKicker":
            captured["schedule_id"] = schedule_id
            return self

        async def schedule_by_time(self, schedule_source: object, run_at: object, **kwargs: object) -> None:
            captured["schedule_source"] = schedule_source
            captured["run_at"] = run_at
            captured["kwargs"] = kwargs

    class FakeTask:
        def kicker(self) -> FakeKicker:
            return FakeKicker()

    monkeypatch.setattr(scheduler, "dynamic_schedule_source", source)
    monkeypatch.setattr(tasks, "retry_failed_forward_task", FakeTask())

    await scheduler.schedule_forward_retry(456, 60)

    source.delete_schedule.assert_awaited_once_with("forward-retry:456")
    assert captured["schedule_id"] == "forward-retry:456"
    assert captured["schedule_source"] is source
    assert captured["kwargs"] == {"failed_forward_id": 456}


@pytest.mark.asyncio
async def test_schedule_openclaw_poll_uses_taskiq_dynamic_schedule(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.operations.taskiq_retry_scheduler as scheduler
    import services.operations.tasks as tasks

    source = AsyncMock()
    captured: dict[str, object] = {}

    class FakeKicker:
        def with_schedule_id(self, schedule_id: str) -> "FakeKicker":
            captured["schedule_id"] = schedule_id
            return self

        async def schedule_by_time(self, schedule_source: object, run_at: object, **kwargs: object) -> None:
            captured["schedule_source"] = schedule_source
            captured["run_at"] = run_at
            captured["kwargs"] = kwargs

    class FakeTask:
        def kicker(self) -> FakeKicker:
            return FakeKicker()

    monkeypatch.setattr(scheduler, "dynamic_schedule_source", source)
    monkeypatch.setattr(tasks, "poll_openclaw_analysis_task", FakeTask())

    await scheduler.schedule_openclaw_poll(789, 30)

    source.delete_schedule.assert_awaited_once_with("openclaw-poll:789")
    assert captured["schedule_id"] == "openclaw-poll:789"
    assert captured["schedule_source"] is source
    assert captured["kwargs"] == {"analysis_id": 789}

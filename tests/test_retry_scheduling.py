from typing import cast
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from services.forwarding.retry_poller import _load_due_retry_ids
from services.operations.taskiq_retry_scheduler import compute_backoff_delay


class FakeScalarResult:
    def __init__(self, values: list[int]) -> None:
        self.values = values

    def all(self) -> list[int]:
        return self.values


class FakeExecuteResult:
    def __init__(self, values: list[int]) -> None:
        self.values = values

    def scalars(self) -> FakeScalarResult:
        return FakeScalarResult(self.values)


class FakeSession:
    def __init__(self, values: list[int]) -> None:
        self.values = values
        self.statement: object | None = None

    async def execute(self, statement: object) -> FakeExecuteResult:
        self.statement = statement
        return FakeExecuteResult(self.values)


def test_compute_backoff_delay_is_bounded() -> None:
    assert compute_backoff_delay(1, initial_delay=30, max_delay=900, multiplier=2.0) == 30
    assert compute_backoff_delay(3, initial_delay=30, max_delay=900, multiplier=2.0) == 120
    assert compute_backoff_delay(99, initial_delay=30, max_delay=900, multiplier=2.0) == 900


@pytest.mark.asyncio
async def test_forward_retry_due_scan_uses_db_query() -> None:
    session = FakeSession([456, 789])

    assert await _load_due_retry_ids(cast(AsyncSession, session), limit=10) == [456, 789]
    assert session.statement is not None
    compiled = str(session.statement)
    assert "failed_forwards.status IN" in compiled
    assert "failed_forwards.next_retry_at" in compiled
    assert "ORDER BY failed_forwards.next_retry_at ASC" in compiled


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

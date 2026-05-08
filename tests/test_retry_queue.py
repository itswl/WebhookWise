from unittest.mock import AsyncMock, patch

import pytest

from services.retry_queue import (
    FORWARD_RETRY_ZSET,
    drain_due_forward_retries,
    enqueue_forward_retry,
)
from services.taskiq_retry_scheduler import compute_backoff_delay


def test_compute_backoff_delay_is_bounded() -> None:
    assert compute_backoff_delay(1, initial_delay=30, max_delay=900, multiplier=2.0) == 30
    assert compute_backoff_delay(3, initial_delay=30, max_delay=900, multiplier=2.0) == 120
    assert compute_backoff_delay(99, initial_delay=30, max_delay=900, multiplier=2.0) == 900


@pytest.mark.asyncio
async def test_enqueue_forward_retry_id_uses_expected_zset() -> None:
    redis = AsyncMock()

    with patch("services.retry_queue.get_redis", return_value=redis), patch(
        "services.retry_queue.time.time", return_value=1000
    ):
        await enqueue_forward_retry(456, 60)

    redis.zadd.assert_any_await(FORWARD_RETRY_ZSET, {"456": 1060})


@pytest.mark.asyncio
async def test_drain_due_retry_ids_filters_invalid_members() -> None:
    async def fake_eval(script: str, numkeys: int, zset: str, now: float, limit: int) -> str:
        assert numkeys == 1
        assert limit == 10
        return "1,bad,2"

    with patch("services.retry_queue.redis_eval_str", side_effect=fake_eval):
        assert await drain_due_forward_retries(limit=10) == [1, 2]


@pytest.mark.asyncio
async def test_schedule_webhook_retry_uses_taskiq_dynamic_schedule(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.taskiq_retry_scheduler as scheduler
    import services.tasks as tasks

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

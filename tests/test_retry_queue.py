from unittest.mock import AsyncMock

import pytest

from services.retry_queue import (
    FORWARD_RETRY_ZSET,
    drain_due_forward_retries,
    enqueue_forward_retry,
)
from services.taskiq_retry_scheduler import compute_backoff_delay


class FakeRedis:
    def __init__(self) -> None:
        self.zsets: dict[str, dict[str, float]] = {}

    async def zadd(self, key: str, mapping: dict[str, float]) -> None:
        self.zsets.setdefault(key, {}).update(mapping)

    async def eval(self, script: str, numkeys: int, zset_key: str, now: float, limit: int) -> str:
        assert numkeys == 1
        due = [
            member
            for member, score in sorted(self.zsets.get(zset_key, {}).items(), key=lambda item: item[1])
            if score <= float(now)
        ][: int(limit)]
        for member in due:
            self.zsets[zset_key].pop(member, None)
        return ",".join(due)


def test_compute_backoff_delay_is_bounded() -> None:
    assert compute_backoff_delay(1, initial_delay=30, max_delay=900, multiplier=2.0) == 30
    assert compute_backoff_delay(3, initial_delay=30, max_delay=900, multiplier=2.0) == 120
    assert compute_backoff_delay(99, initial_delay=30, max_delay=900, multiplier=2.0) == 900


@pytest.mark.asyncio
async def test_forward_retry_queue_drains_only_due_items(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = FakeRedis()
    monkeypatch.setattr("services.retry_queue.get_redis", lambda: redis)
    monkeypatch.setattr("core.redis_client.get_redis", lambda: redis)
    monkeypatch.setattr("services.retry_queue.time.time", lambda: 1000)

    await enqueue_forward_retry(456, 60)
    await enqueue_forward_retry(789, 120)

    assert redis.zsets[FORWARD_RETRY_ZSET] == {"456": 1060, "789": 1120}
    assert await drain_due_forward_retries(limit=10) == []
    assert await drain_due_forward_retries(limit=10) == []

    assert await drain_due_forward_retries(limit=1) == []
    assert redis.zsets[FORWARD_RETRY_ZSET] == {"456": 1060, "789": 1120}

    from services.retry_queue import drain_due_ids

    assert await drain_due_ids(FORWARD_RETRY_ZSET, limit=1, now=1060) == [456]
    assert redis.zsets[FORWARD_RETRY_ZSET] == {"789": 1120}
    assert await drain_due_ids(FORWARD_RETRY_ZSET, limit=10, now=1120) == [789]
    assert redis.zsets[FORWARD_RETRY_ZSET] == {}


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

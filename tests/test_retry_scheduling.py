from unittest.mock import AsyncMock

import pytest

from services.operations.taskiq_retry_scheduler import compute_backoff_delay


def test_compute_backoff_delay_is_bounded() -> None:
    assert compute_backoff_delay(1, initial_delay=30, max_delay=900, multiplier=2.0) == 30
    assert compute_backoff_delay(3, initial_delay=30, max_delay=900, multiplier=2.0) == 120
    assert compute_backoff_delay(99, initial_delay=30, max_delay=900, multiplier=2.0) == 900


def test_compute_openclaw_poll_delay_is_exponential_and_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.config import Config
    from services.operations.taskiq_retry_scheduler import compute_openclaw_poll_delay

    monkeypatch.setattr(Config.openclaw, "OPENCLAW_POLL_INITIAL_DELAY_SECONDS", 10)
    monkeypatch.setattr(Config.openclaw, "OPENCLAW_POLL_BACKOFF_MULTIPLIER", 3.0)
    monkeypatch.setattr(Config.openclaw, "OPENCLAW_POLL_MAX_DELAY_SECONDS", 300)

    assert compute_openclaw_poll_delay(0) == 10
    assert compute_openclaw_poll_delay(1) == 30
    assert compute_openclaw_poll_delay(2) == 90
    assert compute_openclaw_poll_delay(99) == 300
    assert compute_openclaw_poll_delay(100_000) == 300


@pytest.mark.asyncio
async def test_schedule_webhook_ingest_retry_uses_request_id_schedule(monkeypatch: pytest.MonkeyPatch) -> None:
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

    await scheduler.schedule_webhook_ingest_retry(
        delay_seconds=30,
        source="prometheus",
        raw_headers={"x-test": "1"},
        raw_body='{"alertname":"HighCPU"}',
        client_ip="127.0.0.1",
        request_id="req-123",
        received_at="2026-05-13T12:00:00+08:00",
        ingest_retry_count=2,
    )

    source.delete_schedule.assert_awaited_once_with("webhook-ingest-retry:req-123")
    assert captured["schedule_id"] == "webhook-ingest-retry:req-123"
    assert captured["schedule_source"] is source
    assert captured["kwargs"] == {
        "source_name": "prometheus",
        "raw_headers": {"x-test": "1"},
        "raw_body": '{"alertname":"HighCPU"}',
        "client_ip": "127.0.0.1",
        "request_id": "req-123",
        "received_at": "2026-05-13T12:00:00+08:00",
        "ingest_retry_count": 2,
    }


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

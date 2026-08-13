from unittest.mock import AsyncMock

import pytest

from services.operations.taskiq_retry_scheduler import compute_backoff_delay


def test_compute_backoff_delay_is_bounded() -> None:
    assert compute_backoff_delay(1, initial_delay=30, max_delay=900, multiplier=2.0) == 30
    assert compute_backoff_delay(3, initial_delay=30, max_delay=900, multiplier=2.0) == 120
    assert compute_backoff_delay(99, initial_delay=30, max_delay=900, multiplier=2.0) == 900


def test_compute_openclaw_poll_delay_is_exponential_and_bounded(monkeypatch: pytest.MonkeyPatch, temp_config) -> None:
    from services.operations.taskiq_retry_scheduler import compute_deep_analysis_poll_delay

    monkeypatch.setattr(temp_config.deep_analysis, "DEEP_ANALYSIS_POLL_INITIAL_DELAY_SECONDS", 10)
    monkeypatch.setattr(temp_config.deep_analysis, "DEEP_ANALYSIS_POLL_BACKOFF_MULTIPLIER", 3.0)
    monkeypatch.setattr(temp_config.deep_analysis, "DEEP_ANALYSIS_POLL_MAX_DELAY_SECONDS", 300)

    assert compute_deep_analysis_poll_delay(0) == 10
    assert compute_deep_analysis_poll_delay(1) == 30
    assert compute_deep_analysis_poll_delay(2) == 90
    assert compute_deep_analysis_poll_delay(99) == 300
    assert compute_deep_analysis_poll_delay(100_000) == 300


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
    # A fresh suffix per write is what keeps the scheduler from remembering the
    # id as already-sent; the stable part still names the resource.
    assert str(captured["schedule_id"]).startswith("webhook-ingest-retry:req-123:")
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
    monkeypatch.setattr(tasks, "poll_deep_analysis_task", FakeTask())

    await scheduler.schedule_deep_analysis_poll(789, 30)

    source.delete_schedule.assert_awaited_once_with("deep-analysis-poll:789")
    assert str(captured["schedule_id"]).startswith("deep-analysis-poll:789:")
    assert captured["schedule_source"] is source
    assert captured["kwargs"] == {"analysis_id": 789}


def test_schedule_source_can_read_back_the_buckets_it_writes() -> None:
    """A timed schedule must survive a missed minute.

    ListRedisScheduleSource writes minute buckets as "{prefix}:time:{minute}"
    but parses them with key.split(":", 2)[2], so a prefix containing a colon
    makes every bucket unparseable and the sweep for past buckets — the whole
    point of skip_past_schedules=False — silently finds nothing. A deep-analysis
    poll scheduled 20s out was lost this way, leaving the record pending until an
    interval scan re-armed it.
    """
    import datetime

    from core.taskiq_broker import dynamic_schedule_source as source

    minute = datetime.datetime(2026, 8, 13, 12, 14, tzinfo=datetime.UTC)
    assert source._parse_time_key(source._get_time_key(minute)) == minute


def test_writes_go_to_the_fixed_prefix_while_the_legacy_one_is_still_drained() -> None:
    """A restart must not strand schedules written under the old prefix.

    Outbox retries and deep-analysis polls are re-armed from database state by
    the interval scans, but a webhook ingest retry exists only as its schedule
    payload — losing it drops that webhook without a dead letter.
    """
    from core.taskiq_broker import dynamic_schedule_source, legacy_schedule_source, scheduler
    from services.operations.taskiq_retry_scheduler import dynamic_schedule_source as write_target

    assert write_target is dynamic_schedule_source
    assert legacy_schedule_source in scheduler.sources
    assert legacy_schedule_source is not dynamic_schedule_source


@pytest.mark.asyncio
async def test_rescheduling_the_same_resource_never_reuses_a_schedule_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """A re-armed schedule must look new to the scheduler.

    taskiq marks a time schedule id as sent and forgets it only once the id is
    missing from a refresh. A poll that re-arms itself under one stable id
    restores the id immediately, so it is never forgotten and never sent again —
    the record then sits pending until the scheduler process restarts.
    """
    import services.operations.taskiq_retry_scheduler as scheduler
    import services.operations.tasks as tasks

    seen: list[str] = []

    class FakeKicker:
        def with_schedule_id(self, schedule_id: str) -> "FakeKicker":
            seen.append(schedule_id)
            return self

        async def schedule_by_time(self, schedule_source: object, run_at: object, **kwargs: object) -> None:
            return None

    class FakeTask:
        def kicker(self) -> FakeKicker:
            return FakeKicker()

    monkeypatch.setattr(scheduler, "dynamic_schedule_source", AsyncMock())
    monkeypatch.setattr(tasks, "poll_deep_analysis_task", FakeTask(), raising=False)
    monkeypatch.setattr(tasks, "poll_openclaw_analysis_task", FakeTask(), raising=False)

    await scheduler.schedule_deep_analysis_poll(7, 20)
    await scheduler.schedule_deep_analysis_poll(7, 40)

    assert len(seen) == 2
    assert seen[0] != seen[1]
    assert all(s.startswith("deep-analysis-poll:7:") for s in seen)

import httpx
import pytest


@pytest.mark.asyncio
async def test_retry_enqueue_failure_goes_dead_letter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.webhooks import failure_handling

    calls: dict[str, object] = {}

    async def fake_mark_retry(
        event_id: int,
        *,
        max_retries: int,
        error_message: str,
        initial_delay: int | None = None,
        max_delay: int | None = None,
        multiplier: float | None = None,
    ) -> tuple[int, int]:
        calls["mark_retry"] = (event_id, max_retries, error_message, initial_delay, max_delay, multiplier)
        return 1, 30

    async def fake_schedule_webhook_retry(event_id: int, delay_seconds: int) -> None:
        calls["schedule"] = (event_id, delay_seconds)
        raise RuntimeError("redis down")

    async def fake_mark_dead_letter(event_id: int, *, retryable: bool, error_message: str) -> None:
        calls["dead_letter"] = (event_id, retryable, error_message)

    async def fake_notify_dead_letter(event_id: int, retry_count: int, error: Exception) -> None:
        calls["alert"] = (event_id, retry_count, str(error))

    monkeypatch.setattr(failure_handling, "mark_retry", fake_mark_retry)
    monkeypatch.setattr(failure_handling, "mark_dead_letter", fake_mark_dead_letter)

    outcome = await failure_handling.handle_process_exception(
        42,
        RuntimeError("process failed"),
        None,
        policy=failure_handling.WebhookFailurePolicy(
            max_retries=5,
            initial_delay=30,
            max_delay=900,
            backoff_multiplier=2.0,
        ),
        retry_classifier=lambda _: True,
        retry_scheduler=fake_schedule_webhook_retry,
        dead_letter_notifier=fake_notify_dead_letter,
    )

    assert outcome == "dead_letter"
    assert calls["mark_retry"] == (42, 5, "process failed", 30, 900, 2.0)
    assert calls["schedule"] == (42, 30)
    event_id, retryable, error_message = calls["dead_letter"]
    assert event_id == 42
    assert retryable is True
    assert "retry enqueue failed" in str(error_message)
    assert calls["alert"] == (42, 1, "redis down")


@pytest.mark.asyncio
async def test_raw_ingest_failure_schedules_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.operations.taskiq_retry_scheduler as scheduler
    import services.operations.tasks as tasks
    import services.webhooks.ingest_failure as ingest_failure
    from services.webhooks.policies import WebhookFailurePolicy

    calls: dict[str, object] = {}

    async def fake_schedule(**kwargs: object) -> None:
        calls["schedule"] = kwargs

    async def fail_dead_letter(**_: object) -> None:
        raise AssertionError("retryable raw ingest failure should be scheduled, not dead-lettered")

    monkeypatch.setattr(scheduler, "schedule_webhook_ingest_retry", fake_schedule)
    monkeypatch.setattr(ingest_failure, "record_raw_ingest_dead_letter", fail_dead_letter)
    monkeypatch.setattr(
        WebhookFailurePolicy,
        "from_config",
        classmethod(lambda cls: cls(max_retries=5, initial_delay=30, max_delay=900, backoff_multiplier=2.0)),
    )

    await tasks._handle_raw_webhook_failure(
        source="prometheus",
        raw_headers={"x-test": "1"},
        raw_body='{"alertname":"HighCPU"}',
        client_ip="127.0.0.1",
        request_id="req-raw",
        received_at="2026-05-13T12:00:00+08:00",
        ingest_retry_count=0,
        err=httpx.ConnectError("network down"),
    )

    assert calls["schedule"] == {
        "delay_seconds": 30,
        "source": "prometheus",
        "raw_headers": {"x-test": "1"},
        "raw_body": '{"alertname":"HighCPU"}',
        "client_ip": "127.0.0.1",
        "request_id": "req-raw",
        "received_at": "2026-05-13T12:00:00+08:00",
        "ingest_retry_count": 1,
    }


@pytest.mark.asyncio
async def test_raw_ingest_non_retryable_failure_records_dead_letter(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.operations.taskiq_retry_scheduler as scheduler
    import services.operations.tasks as tasks
    import services.webhooks.ingest_failure as ingest_failure

    calls: dict[str, object] = {}

    async def fail_schedule(**_: object) -> None:
        raise AssertionError("non-retryable raw ingest failure should not be scheduled")

    async def fake_dead_letter(**kwargs: object) -> int:
        calls["dead_letter"] = kwargs
        return 123

    monkeypatch.setattr(scheduler, "schedule_webhook_ingest_retry", fail_schedule)
    monkeypatch.setattr(ingest_failure, "record_raw_ingest_dead_letter", fake_dead_letter)

    err = ValueError("bad payload")
    await tasks._handle_raw_webhook_failure(
        source="prometheus",
        raw_headers={"x-test": "1"},
        raw_body="{bad-json",
        client_ip="127.0.0.1",
        request_id="req-bad",
        received_at=None,
        ingest_retry_count=0,
        err=err,
    )

    assert calls["dead_letter"] == {
        "source": "prometheus",
        "raw_headers": {"x-test": "1"},
        "raw_body": "{bad-json",
        "client_ip": "127.0.0.1",
        "request_id": "req-bad",
        "received_at": None,
        "retry_count": 0,
        "retryable": False,
        "err": err,
    }

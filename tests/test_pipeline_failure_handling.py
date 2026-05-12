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

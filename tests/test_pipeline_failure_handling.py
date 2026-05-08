import pytest


@pytest.mark.asyncio
async def test_retry_enqueue_failure_goes_dead_letter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.webhooks.pipeline as pipeline
    from core.config import Config

    monkeypatch.setattr(Config.retry, "WEBHOOK_RETRY_MAX_RETRIES", 5)
    monkeypatch.setattr(Config.retry, "WEBHOOK_RETRY_INITIAL_DELAY", 30)
    monkeypatch.setattr(Config.retry, "WEBHOOK_RETRY_MAX_DELAY", 900)
    monkeypatch.setattr(Config.retry, "WEBHOOK_RETRY_BACKOFF_MULTIPLIER", 2.0)

    calls: dict[str, object] = {}

    async def fake_mark_retry(event_id: int, *, max_retries: int, error_message: str) -> int:
        calls["mark_retry"] = (event_id, max_retries, error_message)
        return 1

    async def fake_schedule_webhook_retry(event_id: int, delay_seconds: int) -> None:
        calls["schedule"] = (event_id, delay_seconds)
        raise RuntimeError("redis down")

    async def fake_mark_dead_letter(event_id: int, *, retryable: bool, error_message: str) -> None:
        calls["dead_letter"] = (event_id, retryable, error_message)

    async def fake_send_dead_letter_alert(event_id: int, retry_count: int, error: Exception) -> None:
        calls["alert"] = (event_id, retry_count, str(error))

    monkeypatch.setattr(pipeline.retry_policy, "should_retry", lambda _: True)
    monkeypatch.setattr(pipeline, "mark_retry", fake_mark_retry)
    monkeypatch.setattr(pipeline, "schedule_webhook_retry", fake_schedule_webhook_retry)
    monkeypatch.setattr(pipeline, "mark_dead_letter", fake_mark_dead_letter)
    monkeypatch.setattr(pipeline, "_send_dead_letter_alert", fake_send_dead_letter_alert)

    outcome = await pipeline._handle_process_exception(42, RuntimeError("process failed"), None)

    assert outcome == "dead_letter"
    assert calls["mark_retry"] == (42, 5, "process failed")
    assert calls["schedule"] == (42, 30)
    event_id, retryable, error_message = calls["dead_letter"]
    assert event_id == 42
    assert retryable is True
    assert "retry enqueue failed" in str(error_message)
    assert calls["alert"] == (42, 1, "redis down")

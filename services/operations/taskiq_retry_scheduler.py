"""TaskIQ-backed one-shot retry scheduling."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.taskiq_broker import dynamic_schedule_source


def compute_backoff_delay(
    attempt: int,
    *,
    initial_delay: int,
    max_delay: int,
    multiplier: float,
) -> int:
    """Return bounded exponential backoff delay in seconds."""
    normalized_attempt = max(1, int(attempt))
    delay = initial_delay * (multiplier ** (normalized_attempt - 1))
    return max(0, int(min(delay, max_delay)))


async def schedule_webhook_retry(event_id: int, delay_seconds: int) -> None:
    """Schedule a single webhook retry through TaskIQ's dynamic scheduler."""
    from services.operations.tasks import process_webhook_task

    schedule_id = f"webhook-retry:{event_id}"
    await dynamic_schedule_source.delete_schedule(schedule_id)
    run_at = datetime.now(timezone.utc) + timedelta(seconds=max(0, int(delay_seconds)))
    await (
        process_webhook_task.kicker()
        .with_schedule_id(schedule_id)
        .schedule_by_time(
            dynamic_schedule_source,
            run_at,
            event_id=event_id,
            client_ip="retry-schedule",
        )
    )


async def schedule_forward_retry(failed_forward_id: int, delay_seconds: int) -> None:
    """Schedule a single failed-forward retry through TaskIQ."""
    from services.operations.tasks import retry_failed_forward_task

    schedule_id = f"forward-retry:{failed_forward_id}"
    await dynamic_schedule_source.delete_schedule(schedule_id)
    run_at = datetime.now(timezone.utc) + timedelta(seconds=max(0, int(delay_seconds)))
    await (
        retry_failed_forward_task.kicker()
        .with_schedule_id(schedule_id)
        .schedule_by_time(
            dynamic_schedule_source,
            run_at,
            failed_forward_id=failed_forward_id,
        )
    )

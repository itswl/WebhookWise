"""One-shot retry scheduling."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from core.logger import get_logger
from core.taskiq_broker import dynamic_schedule_source

if TYPE_CHECKING:
    from services.analysis.openclaw_client import OpenClawPollPolicy

logger = get_logger("taskiq_scheduler")
_SCHEDULING_ERRORS = (OSError, RuntimeError, TimeoutError, ValueError)


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


def compute_openclaw_poll_delay(poll_attempts: int, *, policy: OpenClawPollPolicy | None = None) -> int:
    """Return the next OpenClaw poll delay using bounded exponential backoff."""
    from services.analysis.openclaw_client import OpenClawPollPolicy

    return (policy or OpenClawPollPolicy.from_config()).delay_for_attempt(poll_attempts)


async def _schedule_by_time(schedule_id: str, delay_seconds: int, task: Any, **kwargs: Any) -> None:
    """Shared scheduling helper: delete existing, then schedule at future time."""
    await dynamic_schedule_source.delete_schedule(schedule_id)
    run_at = datetime.now(UTC) + timedelta(seconds=max(0, int(delay_seconds)))
    await task.kicker().with_schedule_id(schedule_id).schedule_by_time(dynamic_schedule_source, run_at, **kwargs)


def _raw_ingest_schedule_id(request_id: str | None, source: str | None, raw_body: str | None) -> str:
    if request_id:
        return f"webhook-ingest-retry:{request_id}"
    seed = f"{source or 'unknown'}\0{raw_body or ''}".encode("utf-8", errors="replace")
    return f"webhook-ingest-retry:{hashlib.sha256(seed).hexdigest()[:32]}"


async def schedule_webhook_ingest_retry(
    *,
    delay_seconds: int,
    source: str,
    raw_headers: dict[str, str],
    raw_body: str,
    client_ip: str,
    request_id: str | None,
    received_at: str | None,
    ingest_retry_count: int,
    traceparent: str | None = None,
) -> None:
    """Schedule a raw webhook retry without requiring a pre-existing DB event."""
    from services.operations.tasks import process_webhook_task

    kwargs: dict[str, object] = {
        "source_name": source,
        "raw_headers": raw_headers,
        "raw_body": raw_body,
        "client_ip": client_ip,
        "request_id": request_id,
        "received_at": received_at,
        "ingest_retry_count": ingest_retry_count,
    }
    if traceparent:
        kwargs["traceparent"] = traceparent
    await _schedule_by_time(
        _raw_ingest_schedule_id(request_id, source, raw_body), delay_seconds, process_webhook_task, **kwargs
    )


async def schedule_forward_outbox(outbox_id: int, delay_seconds: int) -> None:
    from services.operations.tasks import process_forward_outbox_task

    await _schedule_by_time(
        f"forward-outbox:{outbox_id}", delay_seconds, process_forward_outbox_task, outbox_id=outbox_id
    )


async def schedule_openclaw_poll(analysis_id: int, delay_seconds: int) -> None:
    from services.operations.tasks import poll_openclaw_analysis_task

    await _schedule_by_time(
        f"openclaw-poll:{analysis_id}", delay_seconds, poll_openclaw_analysis_task, analysis_id=analysis_id
    )


async def schedule_openclaw_poll_best_effort(analysis_id: int, delay_seconds: int | None = None) -> None:
    try:
        if delay_seconds is None:
            delay_seconds = compute_openclaw_poll_delay(0)
        await schedule_openclaw_poll(analysis_id, delay_seconds)
    except _SCHEDULING_ERRORS as e:
        logger.warning("[OpenClaw] Failed to schedule poll analysis_id=%s error=%s", analysis_id, e)

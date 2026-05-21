"""One-shot retry scheduling."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from core.taskiq_broker import dynamic_schedule_source

if TYPE_CHECKING:
    from services.analysis.openclaw_poll_policy import OpenClawPollPolicy

logger = logging.getLogger("webhook_service.taskiq_scheduler")


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
    from services.analysis.openclaw_poll_policy import OpenClawPollPolicy

    poll_policy = policy or OpenClawPollPolicy.from_config()
    return poll_policy.delay_for_attempt(poll_attempts)


def _raw_ingest_schedule_id(request_id: str | None, source: str | None, raw_body: str | None) -> str:
    if request_id:
        identifier = request_id
    else:
        seed = f"{source or 'unknown'}\0{raw_body or ''}".encode("utf-8", errors="replace")
        identifier = hashlib.sha256(seed).hexdigest()[:32]
    return f"webhook-ingest-retry:{identifier}"


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

    schedule_id = _raw_ingest_schedule_id(request_id, source, raw_body)
    await dynamic_schedule_source.delete_schedule(schedule_id)
    run_at = datetime.now(timezone.utc) + timedelta(seconds=max(0, int(delay_seconds)))
    if traceparent:
        await (
            process_webhook_task.kicker()
            .with_schedule_id(schedule_id)
            .schedule_by_time(
                dynamic_schedule_source,
                run_at,
                source_name=source,
                raw_headers=raw_headers,
                raw_body=raw_body,
                client_ip=client_ip,
                request_id=request_id,
                received_at=received_at,
                ingest_retry_count=ingest_retry_count,
                traceparent=traceparent,
            )
        )
    else:
        await (
            process_webhook_task.kicker()
            .with_schedule_id(schedule_id)
            .schedule_by_time(
                dynamic_schedule_source,
                run_at,
                source_name=source,
                raw_headers=raw_headers,
                raw_body=raw_body,
                client_ip=client_ip,
                request_id=request_id,
                received_at=received_at,
                ingest_retry_count=ingest_retry_count,
            )
        )


async def schedule_forward_outbox(outbox_id: int, delay_seconds: int) -> None:
    """Schedule a single forwarding outbox attempt through TaskIQ."""
    from services.operations.tasks import process_forward_outbox_task

    schedule_id = f"forward-outbox:{outbox_id}"
    await dynamic_schedule_source.delete_schedule(schedule_id)
    run_at = datetime.now(timezone.utc) + timedelta(seconds=max(0, int(delay_seconds)))
    await (
        process_forward_outbox_task.kicker()
        .with_schedule_id(schedule_id)
        .schedule_by_time(
            dynamic_schedule_source,
            run_at,
            outbox_id=outbox_id,
        )
    )


async def schedule_openclaw_poll(analysis_id: int, delay_seconds: int) -> None:
    """Schedule a single OpenClaw result poll through TaskIQ."""
    from services.operations.tasks import poll_openclaw_analysis_task

    schedule_id = f"openclaw-poll:{analysis_id}"
    await dynamic_schedule_source.delete_schedule(schedule_id)
    run_at = datetime.now(timezone.utc) + timedelta(seconds=max(0, int(delay_seconds)))
    await (
        poll_openclaw_analysis_task.kicker()
        .with_schedule_id(schedule_id)
        .schedule_by_time(
            dynamic_schedule_source,
            run_at,
            analysis_id=analysis_id,
        )
    )

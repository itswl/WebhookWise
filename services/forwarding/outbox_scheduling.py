"""Forwarding outbox TaskIQ scheduling helpers."""

from __future__ import annotations

import asyncio

from core.logger import get_logger
from core.observability.metrics import FORWARD_OUTBOX_RECORDS_TOTAL
from services.operations import taskiq_retry_scheduler
from services.operations import tasks as operation_tasks

logger = get_logger("forward_outbox")

_SCHEDULING_ERRORS = (OSError, RuntimeError, TimeoutError)

# Broker enqueues are independent network round-trips; a scanner batch (up to
# ~100 due rows) dispatched one-by-one serializes backlog drain on broker
# latency. Bounded concurrency keeps drain fast without flooding the broker.
_SCHEDULE_CONCURRENCY = 10


async def schedule_forward_outbox_many(outbox_ids: list[int]) -> None:
    """Dispatch immediately; the scheduled scanner picks up missed records."""
    if not outbox_ids:
        return

    semaphore = asyncio.Semaphore(_SCHEDULE_CONCURRENCY)

    async def _dispatch(outbox_id: int) -> None:
        async with semaphore:
            try:
                await operation_tasks.process_forward_outbox_task.kiq(outbox_id=outbox_id)
                FORWARD_OUTBOX_RECORDS_TOTAL.labels("unknown", "scheduled").inc()
            except _SCHEDULING_ERRORS as e:
                FORWARD_OUTBOX_RECORDS_TOTAL.labels("unknown", "schedule_failed").inc()
                logger.warning(
                    "[ForwardOutbox] Immediate scheduling failed id=%s error=%s, will be picked up by the scan task",
                    outbox_id,
                    e,
                )

    await asyncio.gather(*(_dispatch(outbox_id) for outbox_id in outbox_ids))


async def schedule_forward_outbox_retry(outbox_id: int, delay_seconds: int) -> None:
    try:
        await taskiq_retry_scheduler.schedule_forward_outbox(outbox_id, delay_seconds)
        FORWARD_OUTBOX_RECORDS_TOTAL.labels("unknown", "retry_scheduled").inc()
    except _SCHEDULING_ERRORS as e:
        FORWARD_OUTBOX_RECORDS_TOTAL.labels("unknown", "retry_schedule_failed").inc()
        logger.warning(
            "[ForwardOutbox] Delayed scheduling failed id=%s error=%s, will be picked up by the scan task", outbox_id, e
        )

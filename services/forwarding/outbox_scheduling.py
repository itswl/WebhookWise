"""Forwarding outbox TaskIQ scheduling helpers."""

from __future__ import annotations

from core.logger import get_logger
from core.observability.metrics import FORWARD_OUTBOX_RECORDS_TOTAL
from services.operations import taskiq_retry_scheduler
from services.operations import tasks as operation_tasks

logger = get_logger("forward_outbox")

_SCHEDULING_ERRORS = (OSError, RuntimeError, TimeoutError)


async def schedule_forward_outbox_many(outbox_ids: list[int]) -> None:
    """Dispatch immediately; the scheduled scanner picks up missed records."""
    if not outbox_ids:
        return

    for outbox_id in outbox_ids:
        try:
            await operation_tasks.process_forward_outbox_task.kiq(outbox_id=outbox_id)
            FORWARD_OUTBOX_RECORDS_TOTAL.labels("unknown", "scheduled").inc()
        except _SCHEDULING_ERRORS as e:  # noqa: PERF203
            FORWARD_OUTBOX_RECORDS_TOTAL.labels("unknown", "schedule_failed").inc()
            logger.warning("[ForwardOutbox] 即时调度失败 id=%s error=%s，将由扫描任务补扫", outbox_id, e)


async def schedule_forward_outbox_retry(outbox_id: int, delay_seconds: int) -> None:
    try:
        await taskiq_retry_scheduler.schedule_forward_outbox(outbox_id, delay_seconds)
        FORWARD_OUTBOX_RECORDS_TOTAL.labels("unknown", "retry_scheduled").inc()
    except _SCHEDULING_ERRORS as e:
        FORWARD_OUTBOX_RECORDS_TOTAL.labels("unknown", "retry_schedule_failed").inc()
        logger.warning("[ForwardOutbox] 延迟调度失败 id=%s error=%s，将由扫描任务补扫", outbox_id, e)

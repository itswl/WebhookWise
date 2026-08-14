"""Re-arming raw-ingest retries from database state.

Every other delayed path in this service has a scan behind it: outbox rows and
deep-analysis polls are re-armed from what the database says, so a schedule that
is never delivered costs latency rather than the work itself. Ingestion had no
such backstop — the retry existed only as the payload of a delayed task, and
losing that task dropped the webhook silently, without even a dead letter.

This is that backstop. It reconstructs the task from the stored event exactly as
the operator replay does, and it enqueues directly rather than scheduling: the
recovery path must not depend on the mechanism it is recovering from.
"""

from __future__ import annotations

from datetime import timedelta

import sqlalchemy

from core.datetime_utils import utc_isoformat, utcnow
from core.logger import get_logger
from db.session import session_scope
from models import WebhookEvent
from services.webhooks.policies import WebhookRetryPolicy
from services.webhooks.repository import load_event_payload
from services.webhooks.types import WebhookProcessingStatus

logger = get_logger("webhooks.ingest_retry")


def _lease_seconds() -> int:
    """One delivery's worth of head start: long enough for a re-enqueued task to
    run, short enough that a worker crash re-arms it soon."""
    return max(60, int(WebhookRetryPolicy.from_config().max_delay))


async def _enqueue(event: WebhookEvent) -> None:
    from services.operations.tasks import process_webhook_task

    _, raw_body = await load_event_payload(event)
    await process_webhook_task.kiq(
        source_name=event.source or "unknown",
        source_connection_id=event.source_connection_id,
        raw_headers={str(key): str(value) for key, value in dict(event.headers or {}).items()},
        raw_body=raw_body,
        client_ip=event.client_ip or "retry-scan",
        request_id=event.request_id,
        received_at=utc_isoformat(event.timestamp),
        ingest_retry_count=max(0, int(event.retry_count or 0)),
    )


async def run_raw_ingest_retry_scan(limit: int = 100) -> int:
    """Re-enqueue raw ingests whose retry is overdue. Returns how many.

    A row is claimed by pushing next_retry_at forward before its task is
    enqueued, so two scans racing — or a scan racing the delayed task that was
    not lost after all — do not both enqueue on the same tick. Reprocessing one
    body is safe regardless (the save path reuses the row for that request_id and
    ingestion deduplicates by digest); the lease is about not doing it needlessly.
    """
    now = utcnow()
    lease_until = now + timedelta(seconds=_lease_seconds())
    async with session_scope() as session:
        stmt = (
            sqlalchemy.select(WebhookEvent)
            .where(WebhookEvent.processing_status == WebhookProcessingStatus.RETRY)
            .where(WebhookEvent.next_retry_at.is_not(None))
            .where(WebhookEvent.next_retry_at <= now)
            .order_by(WebhookEvent.next_retry_at.asc())
            .limit(max(1, limit))
        )
        events = list((await session.execute(stmt)).scalars().all())
        if not events:
            logger.debug("[IngestRetry] scan found nothing overdue")
            return 0
        for event in events:
            await session.execute(
                sqlalchemy.update(WebhookEvent)
                .where(WebhookEvent.id == event.id)
                .where(WebhookEvent.next_retry_at <= now)
                .values(next_retry_at=lease_until, updated_at=now)
            )

    enqueued = 0
    for event in events:
        try:
            await _enqueue(event)
            enqueued += 1
        except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
            # The lease expires on its own; the next scan picks the row up again.
            logger.warning("[IngestRetry] re-enqueue failed event_id=%s error=%s", event.id, exc)

    if enqueued:
        logger.info(
            "[IngestRetry] re-enqueued %s overdue raw ingest(s) ids=%s",
            enqueued,
            [event.id for event in events],
        )
    return enqueued

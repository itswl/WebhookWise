"""Outbox state transitions and terminalization helpers."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utcnow
from core.logger import get_logger
from core.observability.metrics import FORWARD_OUTBOX_RECORDS_TOTAL
from db.session import session_scope
from models import DeepAnalysis, ForwardOutbox, WebhookEvent
from services.forwarding import outbox_notifications, outbox_scheduling
from services.forwarding.channels import resolve_channel
from services.forwarding.policies import ForwardDeliveryPolicy
from services.notifications import feishu
from services.operations import taskiq_retry_scheduler
from services.webhooks.types import (
    DeepAnalysisStatus,
    ForwardOutboxStatus,
    ForwardResult,
    openclaw_run_id,
    openclaw_session_key,
)

logger = get_logger("forward_outbox_state")

_OUTBOX_NOTIFICATION_ERRORS = (OSError, RuntimeError, SQLAlchemyError, ValueError)

_TERMINAL_OUTBOX_STATUSES = {
    ForwardOutboxStatus.SENT,
    ForwardOutboxStatus.EXPIRED,
    ForwardOutboxStatus.EXHAUSTED,
}


def _is_outbox_terminal(status: ForwardOutboxStatus | str | None) -> bool:
    return status in _TERMINAL_OUTBOX_STATUSES


def _related_webhook_event_ids(record: ForwardOutbox) -> list[int]:
    ids = [record.webhook_event_id, record.original_event_id]
    return list(dict.fromkeys(int(i) for i in ids if i))


async def _expire_outbox_if_old(
    session: AsyncSession,
    outbox_id: int,
    *,
    now: datetime,
    policy: ForwardDeliveryPolicy,
) -> bool:
    if policy.max_delivery_age_seconds <= 0:
        return False

    cutoff = now - timedelta(seconds=policy.max_delivery_age_seconds)
    stmt = (
        update(ForwardOutbox)
        .where(ForwardOutbox.id == outbox_id)
        .where(ForwardOutbox.status.in_([ForwardOutboxStatus.PENDING, ForwardOutboxStatus.RETRYING]))
        .where(ForwardOutbox.created_at < cutoff)
        .values(
            status=ForwardOutboxStatus.EXPIRED,
            next_attempt_at=None,
            updated_at=now,
            last_error=f"forward delivery expired after {policy.max_delivery_age_seconds}s",
        )
        .returning(ForwardOutbox)
    )
    expired = (await session.execute(stmt)).scalar_one_or_none()
    if not expired:
        return False
    FORWARD_OUTBOX_RECORDS_TOTAL.labels(str(expired.target_type or "unknown"), "expired").inc()
    logger.warning(
        "[OutboxScanner] Forward intent expired id=%s event_id=%s age_limit=%ss",
        expired.id,
        expired.webhook_event_id,
        policy.max_delivery_age_seconds,
    )
    return True


async def _claim_outbox(
    outbox_id: int, *, policy: ForwardDeliveryPolicy | None = None
) -> ForwardOutbox | None:
    if not isinstance(policy, ForwardDeliveryPolicy):
        policy = ForwardDeliveryPolicy.from_config()

    now = utcnow()
    async with session_scope() as session:
        if await _expire_outbox_if_old(session, outbox_id, now=now, policy=policy):
            return None
        stmt = (
            update(ForwardOutbox)
            .where(ForwardOutbox.id == outbox_id)
            .where(ForwardOutbox.status.in_([ForwardOutboxStatus.PENDING, ForwardOutboxStatus.RETRYING]))
            .where((ForwardOutbox.next_attempt_at.is_(None)) | (ForwardOutbox.next_attempt_at <= now))
            .values(
                status=ForwardOutboxStatus.PROCESSING,
                attempts=ForwardOutbox.attempts + 1,
                last_attempt_at=now,
                updated_at=now,
            )
            .returning(ForwardOutbox)
        )
        res = await session.execute(stmt)
        return res.scalar_one_or_none()


async def _finalize_outbox_success(record: ForwardOutbox, result: ForwardResult) -> None:
    now = utcnow()
    openclaw_analysis_id: int | None = None
    async with session_scope() as session:
        # Atomically claim the SENT transition: only a row that is not already
        # terminal flips to SENT, and exactly one finalizer wins. A stale-scan
        # requeue can cause a slow delivery to be re-claimed and delivered
        # twice; without this conditional UPDATE both finalizers could pass the
        # read-then-write check and each insert a duplicate DeepAnalysis row.
        claim = await session.execute(
            update(ForwardOutbox)
            .where(ForwardOutbox.id == record.id)
            .where(ForwardOutbox.status.notin_(_TERMINAL_OUTBOX_STATUSES))
            .values(status=ForwardOutboxStatus.SENT, sent_at=now, updated_at=now, last_error=None)
            .returning(ForwardOutbox.id)
        )
        if claim.scalar_one_or_none() is None:
            # Another finalizer already terminalized this record.
            return
        current = await session.get(ForwardOutbox, record.id)
        if current is None:
            return
        current.response_data = dict(result)

        # Ask the channel whether a successful delivery needs a post-commit
        # follow-up record (OpenClaw spawns a DeepAnalysis poll). The state
        # machine owns the session/transaction, so the openclaw-specific row is
        # built here, but the decision is delegated to the channel strategy
        # instead of a hardcoded target_type check.
        if resolve_channel(current).needs_followup_on_success(current, result):
            target_event_id = current.webhook_event_id
            initial_poll_delay = taskiq_retry_scheduler.compute_openclaw_poll_delay(0)
            analysis_record = DeepAnalysis(
                webhook_event_id=target_event_id,
                engine="openclaw",
                openclaw_run_id=openclaw_run_id(result),
                openclaw_session_key=openclaw_session_key(result),
                status=DeepAnalysisStatus.PENDING,
                poll_attempts=0,
                next_poll_at=now + timedelta(seconds=initial_poll_delay),
            )
            session.add(analysis_record)
            await session.flush()
            openclaw_analysis_id = analysis_record.id

        notified_event_ids = _related_webhook_event_ids(current)
        if notified_event_ids:
            await session.execute(
                update(WebhookEvent)
                .where(WebhookEvent.id.in_(notified_event_ids))
                .values(last_notified_at=now, forward_status="sent")
            )

        logger.info(
            "[ForwardOutbox] Forward succeeded id=%s event_id=%s target_type=%s",
            current.id,
            current.webhook_event_id,
            current.target_type,
        )
    if openclaw_analysis_id is not None:
        await taskiq_retry_scheduler.schedule_openclaw_poll_best_effort(openclaw_analysis_id)


async def _finalize_outbox_failure(
    outbox_id: int, error_msg: str, *, policy: ForwardDeliveryPolicy | None = None
) -> None:
    now = utcnow()
    retry_outbox_id: int | None = None
    retry_delay: int | None = None
    exhausted_record: ForwardOutbox | None = None

    if policy is None:
        policy = ForwardDeliveryPolicy.from_config()

    async with session_scope() as session:
        record = await session.get(ForwardOutbox, outbox_id)
        if not record or _is_outbox_terminal(record.status):
            return
        record.last_error = error_msg[:2000]
        record.updated_at = now
        if record.attempts >= record.max_attempts:
            record.status = ForwardOutboxStatus.EXHAUSTED
            record.next_attempt_at = None
            logger.warning(
                "[ForwardOutbox] Forward exhausted id=%s attempts=%s/%s error=%s",
                record.id,
                record.attempts,
                record.max_attempts,
                error_msg,
            )
            FORWARD_OUTBOX_RECORDS_TOTAL.labels(str(record.target_type or "unknown"), "exhausted").inc()
            exhausted_record = record
            evt_ids = _related_webhook_event_ids(record)
            if evt_ids:
                await session.execute(
                    update(WebhookEvent).where(WebhookEvent.id.in_(evt_ids)).values(forward_status="failed")
                )
        else:
            delay = policy.delay_for_attempt(record.attempts)
            record.status = ForwardOutboxStatus.RETRYING
            record.next_attempt_at = now + timedelta(seconds=delay)
            retry_outbox_id = record.id
            retry_delay = delay
            FORWARD_OUTBOX_RECORDS_TOTAL.labels(str(record.target_type or "unknown"), "retrying").inc()
            logger.info("[ForwardOutbox] Forward failed id=%s delay=%ss error=%s", record.id, delay, error_msg)

    if exhausted_record is not None:
        try:
            exhausted_event_type = str(getattr(exhausted_record, "event_type", "") or "")
            if exhausted_event_type != "outbox_exhausted":
                await outbox_notifications.enqueue_forward_notification(
                    event_type="outbox_exhausted",
                    formatted_payload=feishu.build_delivery_exhausted_card(exhausted_record),
                    webhook_id=exhausted_record.webhook_event_id,
                )
        except _OUTBOX_NOTIFICATION_ERRORS as exc:
            logger.warning(
                "[ForwardOutbox] Failed to enqueue EXHAUSTED notification id=%s error=%s",
                outbox_id,
                exc,
            )
    if retry_outbox_id is not None and retry_delay is not None:
        await outbox_scheduling.schedule_forward_outbox_retry(retry_outbox_id, retry_delay)

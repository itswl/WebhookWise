"""Transactional forwarding outbox.

The webhook pipeline writes forwarding intents before any HTTP side effect.
Workers consume those intents asynchronously, giving the system an auditable,
recoverable at-least-once delivery path.
"""

from __future__ import annotations

import contextlib
import hashlib
import time
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.logger import get_logger
from core.observability.metrics import (
    FORWARD_OUTBOX_BACKLOG_AGE_SECONDS,
    FORWARD_OUTBOX_PROCESS_DURATION_SECONDS,
    FORWARD_OUTBOX_RECORDS_TOTAL,
)
from core.observability.tracing import set_span_error
from core.observability.tracing import span as otel_span
from db.session import session_scope
from models import ForwardOutbox, WebhookEvent
from services.forwarding.policies import ForwardOutboxPolicy
from services.webhooks.types import (
    AnalysisResult,
    DeepAnalysisStatus,
    ForwardDecision,
    ForwardOutboxStatus,
    ForwardResult,
    ForwardRuleTarget,
    WebhookData,
)

logger = get_logger("forward_outbox")


def _rule_id(rule: ForwardRuleTarget) -> int | None:
    raw = rule.get("id")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        with contextlib.suppress(ValueError):
            return int(raw)
    return None


def _idempotency_key(
    *,
    webhook_id: int,
    rule_id: int | None,
    target_type: str,
    target_url: str,
    is_periodic_reminder: bool,
) -> str:
    raw = f"{webhook_id}|{rule_id or 'default'}|{target_type}|{target_url}|{int(is_periodic_reminder)}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"forward:{webhook_id}:{digest[:32]}"


def _iter_target_rules(decision: ForwardDecision, policy: ForwardOutboxPolicy) -> list[ForwardRuleTarget]:
    if decision.matched_rules:
        return list(decision.matched_rules)
    return [policy.default_rule()]


async def create_forward_outbox_records(
    session: AsyncSession,
    *,
    decision: ForwardDecision,
    full_data: dict[str, Any],
    analysis: AnalysisResult,
    webhook_id: int,
    orig_id: int | None,
    policy: ForwardOutboxPolicy | None = None,
) -> list[int]:
    """Create forwarding intents inside the caller's DB transaction."""
    if not decision.should_forward:
        return []

    policy = policy or ForwardOutboxPolicy.from_config()
    created_ids: list[int] = []
    now = datetime.now()
    max_attempts = policy.max_attempts
    for rule in _iter_target_rules(decision, policy):
        target_type = str(rule.get("target_type", "webhook") or "webhook")
        target_url = str(rule.get("target_url", "") or "")
        if target_type != "openclaw" and not target_url:
            logger.warning("[ForwardOutbox] 规则 '%s' target_url 为空，跳过意图创建", rule.get("name", rule.get("id")))
            FORWARD_OUTBOX_RECORDS_TOTAL.labels(target_type, "skipped_empty_target").inc()
            continue

        rule_id = _rule_id(rule)
        key = _idempotency_key(
            webhook_id=webhook_id,
            rule_id=rule_id,
            target_type=target_type,
            target_url=target_url,
            is_periodic_reminder=decision.is_periodic_reminder,
        )
        existing = (
            await session.execute(select(ForwardOutbox.id).where(ForwardOutbox.idempotency_key == key))
        ).scalar_one_or_none()
        if existing is not None:
            logger.info("[ForwardOutbox] 意图已存在 key=%s id=%s", key, existing)
            FORWARD_OUTBOX_RECORDS_TOTAL.labels(target_type, "duplicate").inc()
            continue

        record = ForwardOutbox(
            idempotency_key=key,
            webhook_event_id=webhook_id,
            original_event_id=orig_id,
            forward_rule_id=rule_id,
            rule_name=str(rule.get("name") or rule.get("id") or "default"),
            target_type=target_type,
            target_url=target_url,
            target_name=str(rule.get("target_name", "") or ""),
            is_periodic_reminder=decision.is_periodic_reminder,
            status=ForwardOutboxStatus.PENDING,
            attempts=0,
            max_attempts=max_attempts,
            next_attempt_at=now,
            forward_data=full_data,
            analysis_result=analysis,
            created_at=now,
            updated_at=now,
        )
        session.add(record)
        await session.flush()
        created_ids.append(record.id)
        FORWARD_OUTBOX_RECORDS_TOTAL.labels(target_type, "created").inc()
        logger.info(
            "[ForwardOutbox] 已创建转发意图 id=%s event_id=%s target_type=%s", record.id, webhook_id, target_type
        )
    return created_ids


async def schedule_forward_outbox_many(outbox_ids: list[int]) -> None:
    """Dispatch immediately; the scheduled scanner picks up missed records."""
    if not outbox_ids:
        return

    from services.operations.tasks import process_forward_outbox_task

    for outbox_id in outbox_ids:
        try:
            await process_forward_outbox_task.kiq(outbox_id=outbox_id)
            FORWARD_OUTBOX_RECORDS_TOTAL.labels("unknown", "scheduled").inc()
        except Exception as e:  # noqa: PERF203
            FORWARD_OUTBOX_RECORDS_TOTAL.labels("unknown", "schedule_failed").inc()
            logger.warning("[ForwardOutbox] 即时调度失败 id=%s error=%s，将由扫描任务补扫", outbox_id, e)


async def schedule_forward_outbox_retry(outbox_id: int, delay_seconds: int) -> None:
    try:
        from services.operations.taskiq_retry_scheduler import schedule_forward_outbox

        await schedule_forward_outbox(outbox_id, delay_seconds)
        FORWARD_OUTBOX_RECORDS_TOTAL.labels("unknown", "retry_scheduled").inc()
    except Exception as e:
        FORWARD_OUTBOX_RECORDS_TOTAL.labels("unknown", "retry_schedule_failed").inc()
        logger.warning("[ForwardOutbox] 延迟调度失败 id=%s error=%s，将由扫描任务补扫", outbox_id, e)


def _expires_before(now: datetime, policy: ForwardOutboxPolicy) -> datetime | None:
    if policy.max_delivery_age_seconds <= 0:
        return None
    return now - timedelta(seconds=policy.max_delivery_age_seconds)


async def _expire_outbox_if_old(
    session: AsyncSession,
    outbox_id: int,
    *,
    now: datetime,
    policy: ForwardOutboxPolicy,
) -> bool:
    cutoff = _expires_before(now, policy)
    if cutoff is None:
        return False
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
        "[ForwardOutbox] 转发意图已过期 id=%s event_id=%s age_limit=%ss",
        expired.id,
        expired.webhook_event_id,
        policy.max_delivery_age_seconds,
    )
    return True


async def _claim_outbox(outbox_id: int, *, policy: ForwardOutboxPolicy | None = None) -> ForwardOutbox | None:
    now = datetime.now()
    policy = policy or ForwardOutboxPolicy.from_config()
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


def _is_forward_success(result: ForwardResult) -> bool:
    return result.get("status") == "success" or bool(result.get("_pending"))


async def process_forward_outbox_by_id(outbox_id: int) -> None:
    started = time.perf_counter()
    target_type = "unknown"
    status = "not_claimed"
    record = await _claim_outbox(outbox_id)
    if not record:
        FORWARD_OUTBOX_RECORDS_TOTAL.labels(target_type, status).inc()
        FORWARD_OUTBOX_PROCESS_DURATION_SECONDS.labels(target_type, status).observe(time.perf_counter() - started)
        return
    target_type = str(record.target_type or "unknown")

    with otel_span(
        "forward.outbox.process",
        {
            "event_id": record.webhook_event_id,
            "forward.outbox.id": record.id,
            "forward.target_type": target_type,
            "forward.status": str(record.status or "unknown"),
        },
    ) as outbox_span:
        try:
            result = await _send_outbox_record(record)
        except Exception as e:
            status = "failed"
            set_span_error(outbox_span, e)
            await _finalize_outbox_failure(record.id, str(e))
            FORWARD_OUTBOX_RECORDS_TOTAL.labels(target_type, status).inc()
            FORWARD_OUTBOX_PROCESS_DURATION_SECONDS.labels(target_type, status).observe(time.perf_counter() - started)
            return

        if _is_forward_success(result):
            status = "sent"
            await _finalize_outbox_success(record, result)
        else:
            status = "failed"
            await _finalize_outbox_failure(
                record.id, f"forward status={result.get('status')}: {result.get('message', '')}"
            )
        if outbox_span is not None:
            outbox_span.set_attribute("forward.status", status)
    FORWARD_OUTBOX_RECORDS_TOTAL.labels(target_type, status).inc()
    FORWARD_OUTBOX_PROCESS_DURATION_SECONDS.labels(target_type, status).observe(time.perf_counter() - started)


async def _send_outbox_record(record: ForwardOutbox) -> ForwardResult:
    forward_data = cast(WebhookData, dict(record.forward_data or {}))
    analysis = cast(AnalysisResult, dict(record.analysis_result or {}))
    if record.target_type == "openclaw":
        from services.forwarding.openclaw import forward_to_openclaw

        return await forward_to_openclaw(forward_data, analysis)

    from services.forwarding.remote import forward_to_remote

    return await forward_to_remote(
        webhook_data=forward_data,
        analysis_result=analysis,
        target_url=record.target_url,
        is_periodic_reminder=bool(record.is_periodic_reminder),
    )


async def _finalize_outbox_success(record: ForwardOutbox, result: ForwardResult) -> None:
    now = datetime.now()
    openclaw_analysis_id: int | None = None
    async with session_scope() as session:
        current = await session.get(ForwardOutbox, record.id)
        if not current or current.status in (
            ForwardOutboxStatus.SENT,
            ForwardOutboxStatus.EXPIRED,
            ForwardOutboxStatus.EXHAUSTED,
        ):
            return
        current.status = ForwardOutboxStatus.SENT
        current.sent_at = now
        current.updated_at = now
        current.last_error = None
        current.response_data = dict(result)

        if current.target_type == "openclaw" and result.get("_pending"):
            from models import DeepAnalysis
            from services.operations.taskiq_retry_scheduler import compute_openclaw_poll_delay

            target_event_id = current.webhook_event_id
            initial_poll_delay = compute_openclaw_poll_delay(0)
            analysis_record = DeepAnalysis(
                webhook_event_id=target_event_id,
                engine="openclaw",
                openclaw_run_id=str(result.get("_openclaw_run_id", "")),
                openclaw_session_key=str(result.get("_openclaw_session_key", "")),
                status=DeepAnalysisStatus.PENDING,
                poll_attempts=0,
                next_poll_at=now + timedelta(seconds=initial_poll_delay),
            )
            session.add(analysis_record)
            await session.flush()
            openclaw_analysis_id = analysis_record.id

        notified_event_id = current.original_event_id or current.webhook_event_id
        if notified_event_id:
            await session.execute(
                update(WebhookEvent).where(WebhookEvent.id == notified_event_id).values(last_notified_at=now)
            )

        logger.info(
            "[ForwardOutbox] 转发成功 id=%s event_id=%s target_type=%s",
            current.id,
            current.webhook_event_id,
            current.target_type,
        )
    if openclaw_analysis_id is not None:
        await _schedule_openclaw_poll_best_effort(openclaw_analysis_id)


async def _schedule_openclaw_poll_best_effort(analysis_id: int) -> None:
    try:
        from services.operations.taskiq_retry_scheduler import compute_openclaw_poll_delay, schedule_openclaw_poll

        await schedule_openclaw_poll(analysis_id, compute_openclaw_poll_delay(0))
    except Exception as e:
        logger.warning("[ForwardOutbox] OpenClaw poll 调度失败 analysis_id=%s error=%s", analysis_id, e)


async def _finalize_outbox_failure(
    outbox_id: int, error_msg: str, *, policy: ForwardOutboxPolicy | None = None
) -> None:
    now = datetime.now()
    retry_outbox_id: int | None = None
    retry_delay: int | None = None
    policy = policy or ForwardOutboxPolicy.from_config()
    async with session_scope() as session:
        record = await session.get(ForwardOutbox, outbox_id)
        if not record or record.status in (
            ForwardOutboxStatus.SENT,
            ForwardOutboxStatus.EXPIRED,
            ForwardOutboxStatus.EXHAUSTED,
        ):
            return
        record.last_error = error_msg[:2000]
        record.updated_at = now
        if record.attempts >= record.max_attempts:
            record.status = ForwardOutboxStatus.EXHAUSTED
            logger.warning(
                "[ForwardOutbox] 转发耗尽 id=%s attempts=%s/%s error=%s",
                record.id,
                record.attempts,
                record.max_attempts,
                error_msg,
            )
            FORWARD_OUTBOX_RECORDS_TOTAL.labels(str(record.target_type or "unknown"), "exhausted").inc()
            return

        delay = policy.delay_for_attempt(record.attempts)
        record.status = ForwardOutboxStatus.RETRYING
        record.next_attempt_at = now + timedelta(seconds=delay)
        retry_outbox_id = record.id
        retry_delay = delay
        FORWARD_OUTBOX_RECORDS_TOTAL.labels(str(record.target_type or "unknown"), "retrying").inc()
        logger.info("[ForwardOutbox] 转发失败 id=%s delay=%ss error=%s", record.id, delay, error_msg)
    if retry_outbox_id is not None and retry_delay is not None:
        await schedule_forward_outbox_retry(retry_outbox_id, retry_delay)


async def _expire_due_outboxes(session: AsyncSession, *, now: datetime, policy: ForwardOutboxPolicy, limit: int) -> int:
    cutoff = _expires_before(now, policy)
    if cutoff is None or limit <= 0:
        return 0
    stmt = (
        update(ForwardOutbox)
        .where(
            ForwardOutbox.id.in_(
                select(ForwardOutbox.id)
                .where(
                    ForwardOutbox.status.in_(
                        [ForwardOutboxStatus.PENDING, ForwardOutboxStatus.RETRYING, ForwardOutboxStatus.PROCESSING]
                    )
                )
                .where(ForwardOutbox.created_at < cutoff)
                .order_by(ForwardOutbox.created_at.asc(), ForwardOutbox.id.asc())
                .limit(limit)
            )
        )
        .values(
            status=ForwardOutboxStatus.EXPIRED,
            next_attempt_at=None,
            updated_at=now,
            last_error=f"forward delivery expired after {policy.max_delivery_age_seconds}s",
        )
        .returning(ForwardOutbox)
    )
    expired_records = list((await session.execute(stmt)).scalars().all())
    if expired_records:
        for record in expired_records:
            FORWARD_OUTBOX_RECORDS_TOTAL.labels(str(record.target_type or "unknown"), "expired").inc()
        logger.warning("[ForwardOutbox] 批量过期转发意图 count=%s", len(expired_records))
    return len(expired_records)


async def _refresh_outbox_backlog_metrics(session: AsyncSession, *, now: datetime) -> None:
    active_statuses = [
        ForwardOutboxStatus.PENDING,
        ForwardOutboxStatus.RETRYING,
        ForwardOutboxStatus.PROCESSING,
    ]
    rows = (
        await session.execute(
            select(
                ForwardOutbox.target_type,
                ForwardOutbox.status,
                func.min(ForwardOutbox.created_at),
            )
            .where(ForwardOutbox.status.in_(active_statuses))
            .group_by(ForwardOutbox.target_type, ForwardOutbox.status)
        )
    ).all()
    max_age = 0.0
    for target_type, status, oldest_created_at in rows:
        if oldest_created_at is None:
            continue
        status_value = status.value if isinstance(status, ForwardOutboxStatus) else str(status or "unknown")
        age_seconds = max(0.0, (now - oldest_created_at).total_seconds())
        max_age = max(max_age, age_seconds)
        FORWARD_OUTBOX_BACKLOG_AGE_SECONDS.labels(str(target_type or "unknown"), status_value).set(age_seconds)
    FORWARD_OUTBOX_BACKLOG_AGE_SECONDS.labels("all", "active").set(max_age)


async def run_forward_outbox_scan(limit: int = 100, *, policy: ForwardOutboxPolicy | None = None) -> int:
    """Queue due outbox records and recover stale processing rows."""
    now = datetime.now()
    policy = policy or ForwardOutboxPolicy.from_config()
    stale_before = now - timedelta(seconds=policy.stale_processing_threshold_seconds)
    async with session_scope() as session:
        expired_count = await _expire_due_outboxes(session, now=now, policy=policy, limit=limit)
        await _refresh_outbox_backlog_metrics(session, now=now)
        await session.execute(
            update(ForwardOutbox)
            .where(ForwardOutbox.status == ForwardOutboxStatus.PROCESSING)
            .where(ForwardOutbox.updated_at < stale_before)
            .values(
                status=ForwardOutboxStatus.RETRYING,
                next_attempt_at=now,
                updated_at=now,
                last_error="recovered_stale_processing",
            )
        )
        stmt = (
            select(ForwardOutbox.id)
            .where(ForwardOutbox.status.in_([ForwardOutboxStatus.PENDING, ForwardOutboxStatus.RETRYING]))
            .where((ForwardOutbox.next_attempt_at.is_(None)) | (ForwardOutbox.next_attempt_at <= now))
            .order_by(ForwardOutbox.next_attempt_at.asc(), ForwardOutbox.id.asc())
            .limit(limit)
        )
        ids = list((await session.execute(stmt)).scalars().all())
    await schedule_forward_outbox_many(ids)
    FORWARD_OUTBOX_RECORDS_TOTAL.labels("unknown", "scan_queued").inc(len(ids))
    if expired_count:
        FORWARD_OUTBOX_RECORDS_TOTAL.labels("unknown", "scan_expired").inc(expired_count)
    return expired_count + len(ids)

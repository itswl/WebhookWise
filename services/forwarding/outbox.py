"""Transactional forwarding outbox.

The webhook pipeline writes forwarding intents before any HTTP side effect.
Workers consume those intents asynchronously, giving the system an auditable,
recoverable at-least-once delivery path.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import Config
from db.session import session_scope
from models import FailedForward, ForwardOutbox, WebhookEvent
from services.operations.taskiq_retry_scheduler import compute_backoff_delay
from services.webhooks.types import ForwardDecision

logger = logging.getLogger("webhook_service.forward_outbox")


def _rule_id(rule: dict[str, Any]) -> int | None:
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


def _iter_target_rules(decision: ForwardDecision) -> list[dict[str, Any]]:
    if decision.matched_rules:
        return [dict(r) for r in decision.matched_rules]
    return [{"name": "default", "target_url": Config.ai.FORWARD_URL, "target_type": "webhook"}]


async def create_forward_outbox_records(
    session: AsyncSession,
    *,
    decision: ForwardDecision,
    full_data: dict[str, Any],
    analysis: dict[str, Any],
    webhook_id: int,
    orig_id: int | None,
) -> list[int]:
    """Create forwarding intents inside the caller's DB transaction."""
    if not decision.should_forward:
        return []

    created_ids: list[int] = []
    now = datetime.now()
    max_attempts = max(1, Config.retry.FORWARD_RETRY_MAX_RETRIES + 1)
    for rule in _iter_target_rules(decision):
        target_type = str(rule.get("target_type", "webhook") or "webhook")
        target_url = str(rule.get("target_url", "") or "")
        if target_type != "openclaw" and not target_url:
            logger.warning("[ForwardOutbox] 规则 '%s' target_url 为空，跳过意图创建", rule.get("name", rule.get("id")))
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
            status="pending",
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
        logger.info(
            "[ForwardOutbox] 已创建转发意图 id=%s event_id=%s target_type=%s", record.id, webhook_id, target_type
        )
    return created_ids


async def schedule_forward_outbox_many(outbox_ids: list[int]) -> None:
    """Best-effort immediate dispatch; scheduled scanner is the durable fallback."""
    if not outbox_ids:
        return
    from services.operations.tasks import process_forward_outbox_task

    for outbox_id in outbox_ids:
        try:
            await process_forward_outbox_task.kiq(outbox_id=outbox_id)
        except Exception as e:  # noqa: PERF203
            logger.warning("[ForwardOutbox] 即时调度失败 id=%s error=%s，将由扫描任务兜底", outbox_id, e)


async def schedule_forward_outbox_retry(outbox_id: int, delay_seconds: int) -> None:
    try:
        from services.operations.taskiq_retry_scheduler import schedule_forward_outbox

        await schedule_forward_outbox(outbox_id, delay_seconds)
    except Exception as e:
        logger.warning("[ForwardOutbox] 延迟调度失败 id=%s error=%s，将由扫描任务兜底", outbox_id, e)


async def _claim_outbox(outbox_id: int) -> ForwardOutbox | None:
    now = datetime.now()
    async with session_scope() as session:
        stmt = (
            update(ForwardOutbox)
            .where(ForwardOutbox.id == outbox_id)
            .where(ForwardOutbox.status.in_(["pending", "retrying"]))
            .where((ForwardOutbox.next_attempt_at.is_(None)) | (ForwardOutbox.next_attempt_at <= now))
            .values(
                status="processing",
                attempts=ForwardOutbox.attempts + 1,
                last_attempt_at=now,
                updated_at=now,
            )
            .returning(ForwardOutbox)
        )
        res = await session.execute(stmt)
        return res.scalar_one_or_none()


def _is_forward_success(result: dict[str, Any]) -> bool:
    return result.get("status") == "success" or bool(result.get("_pending"))


async def process_forward_outbox_by_id(outbox_id: int) -> None:
    record = await _claim_outbox(outbox_id)
    if not record:
        return

    try:
        result = await _send_outbox_record(record)
    except Exception as e:
        await _finalize_outbox_failure(record.id, str(e))
        return

    if _is_forward_success(result):
        await _finalize_outbox_success(record, result)
    else:
        await _finalize_outbox_failure(record.id, f"forward status={result.get('status')}: {result.get('message', '')}")


async def _send_outbox_record(record: ForwardOutbox) -> dict[str, Any]:
    if record.target_type == "openclaw":
        from services.forwarding.forward import forward_to_openclaw

        return await forward_to_openclaw(dict(record.forward_data or {}), dict(record.analysis_result or {}))

    from services.forwarding.forward import forward_to_remote

    return await forward_to_remote(
        webhook_data=dict(record.forward_data or {}),
        analysis_result=dict(record.analysis_result or {}),
        target_url=record.target_url,
        is_periodic_reminder=bool(record.is_periodic_reminder),
    )


async def _finalize_outbox_success(record: ForwardOutbox, result: dict[str, Any]) -> None:
    now = datetime.now()
    openclaw_analysis_id: int | None = None
    async with session_scope() as session:
        current = await session.get(ForwardOutbox, record.id)
        if not current or current.status == "sent":
            return
        current.status = "sent"
        current.sent_at = now
        current.updated_at = now
        current.last_error = None
        current.response_data = result

        if current.target_type == "openclaw" and result.get("_pending"):
            from models import DeepAnalysis
            from services.operations.taskiq_retry_scheduler import compute_openclaw_poll_delay

            target_event_id = current.original_event_id or current.webhook_event_id
            initial_poll_delay = compute_openclaw_poll_delay(0)
            analysis_record = DeepAnalysis(
                webhook_event_id=target_event_id,
                engine="openclaw",
                openclaw_run_id=str(result.get("_openclaw_run_id", "")),
                openclaw_session_key=str(result.get("_openclaw_session_key", "")),
                status="pending",
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


async def _finalize_outbox_failure(outbox_id: int, error_msg: str) -> None:
    now = datetime.now()
    retry_outbox_id: int | None = None
    retry_delay: int | None = None
    async with session_scope() as session:
        record = await session.get(ForwardOutbox, outbox_id)
        if not record or record.status == "sent":
            return
        record.last_error = error_msg[:2000]
        record.updated_at = now
        if record.attempts >= record.max_attempts:
            record.status = "exhausted"
            await _record_exhausted_failed_forward(session, record)
            logger.warning(
                "[ForwardOutbox] 转发耗尽 id=%s attempts=%s/%s error=%s",
                record.id,
                record.attempts,
                record.max_attempts,
                error_msg,
            )
            return

        delay = compute_backoff_delay(
            record.attempts,
            initial_delay=Config.retry.FORWARD_RETRY_INITIAL_DELAY,
            max_delay=Config.retry.FORWARD_RETRY_MAX_DELAY,
            multiplier=Config.retry.FORWARD_RETRY_BACKOFF_MULTIPLIER,
        )
        record.status = "retrying"
        record.next_attempt_at = now + timedelta(seconds=delay)
        retry_outbox_id = record.id
        retry_delay = delay
        logger.info("[ForwardOutbox] 转发失败 id=%s delay=%ss error=%s", record.id, delay, error_msg)
    if retry_outbox_id is not None and retry_delay is not None:
        await schedule_forward_outbox_retry(retry_outbox_id, retry_delay)


async def _record_exhausted_failed_forward(session: AsyncSession, record: ForwardOutbox) -> None:
    failed = FailedForward(
        webhook_event_id=record.webhook_event_id,
        forward_rule_id=record.forward_rule_id,
        target_url=record.target_url,
        target_type=record.target_type,
        status="exhausted",
        failure_reason="outbox_exhausted",
        error_message=record.last_error,
        retry_count=record.attempts,
        max_retries=record.max_attempts,
        next_retry_at=None,
        last_retry_at=record.last_attempt_at,
        forward_data=record.forward_data,
        forward_headers=None,
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )
    session.add(failed)


async def run_forward_outbox_scan(limit: int = 100) -> int:
    """Queue due outbox records and recover stale processing rows."""
    now = datetime.now()
    stale_before = now - timedelta(seconds=Config.server.RECOVERY_POLLER_STUCK_THRESHOLD_SECONDS)
    async with session_scope() as session:
        await session.execute(
            update(ForwardOutbox)
            .where(ForwardOutbox.status == "processing")
            .where(ForwardOutbox.updated_at < stale_before)
            .values(status="retrying", next_attempt_at=now, updated_at=now, last_error="recovered_stale_processing")
        )
        stmt = (
            select(ForwardOutbox.id)
            .where(ForwardOutbox.status.in_(["pending", "retrying"]))
            .where((ForwardOutbox.next_attempt_at.is_(None)) | (ForwardOutbox.next_attempt_at <= now))
            .order_by(ForwardOutbox.next_attempt_at.asc(), ForwardOutbox.id.asc())
            .limit(limit)
        )
        ids = list((await session.execute(stmt)).scalars().all())
    await schedule_forward_outbox_many(ids)
    return len(ids)

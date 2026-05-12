from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

import orjson
from sqlalchemy import select, update

from core.compression import decompress_payload_async
from db.session import session_scope
from models import DeepAnalysis, ForwardRule, WebhookEvent
from services.analysis.noise_reduction import AlertContext
from services.webhooks.decisioning import ForwardRuleSnapshot, normalize_importance
from services.webhooks.state_machine import allowed_sources
from services.webhooks.types import WebhookProcessingStatus

_ANALYZING_SOURCES = allowed_sources(WebhookProcessingStatus.ANALYZING)


@dataclass(slots=True)
class EventEnvelope:
    headers: dict[str, Any]
    payload: dict[str, Any] | None
    raw_body: bytes
    source: str | None
    event_ts: str | None
    request_id: str | None = None


@dataclass(frozen=True)
class DuplicateCheckResult:
    is_duplicate: bool
    original_event: WebhookEvent | None
    beyond_window: bool
    last_beyond_window_event: WebhookEvent | None


async def check_duplicate_event(
    alert_hash: str,
    *,
    session: Any,
    time_window_hours: int = 24,
) -> DuplicateCheckResult:
    """Check duplicate state for one alert hash within a time window."""
    now = datetime.now()
    threshold = now - timedelta(hours=time_window_hours)

    recent_stmt = (
        select(WebhookEvent)
        .filter(WebhookEvent.alert_hash == alert_hash, WebhookEvent.timestamp >= threshold)
        .order_by(WebhookEvent.timestamp.desc())
        .limit(1)
    )
    recent_res = await session.execute(recent_stmt)
    any_event = recent_res.scalar_one_or_none()

    last_beyond_stmt = (
        select(WebhookEvent)
        .filter(WebhookEvent.alert_hash == alert_hash, WebhookEvent.beyond_window.is_(True))
        .order_by(WebhookEvent.timestamp.desc())
        .limit(1)
    )
    last_beyond_res = await session.execute(last_beyond_stmt)
    last_beyond = last_beyond_res.scalar_one_or_none()

    if any_event:
        original_id = any_event.duplicate_of if any_event.is_duplicate else any_event.id
        original = await session.get(WebhookEvent, original_id) if original_id else any_event
        if original is None:
            original = any_event
        window_start = last_beyond.timestamp if last_beyond else original.timestamp
        is_within = (now - window_start).total_seconds() / 3600 <= time_window_hours
        return DuplicateCheckResult(True, original, not is_within, last_beyond)

    history_stmt = (
        select(WebhookEvent)
        .filter(WebhookEvent.alert_hash == alert_hash, WebhookEvent.is_duplicate.is_(False))
        .order_by(WebhookEvent.timestamp.desc())
        .limit(1)
    )
    history_res = await session.execute(history_stmt)
    history = history_res.scalar_one_or_none()

    if history:
        return DuplicateCheckResult(False, history, True, last_beyond)
    return DuplicateCheckResult(False, None, False, None)


async def load_event_payload(event: WebhookEvent) -> tuple[dict[str, Any] | None, str]:
    raw_text = await decompress_payload_async(event.raw_payload) or ""
    parsed_data = event.parsed_data
    if parsed_data is None and raw_text:
        try:
            loaded = orjson.loads(raw_text)
            parsed_data = loaded if isinstance(loaded, dict) else None
        except orjson.JSONDecodeError:
            parsed_data = None
    return parsed_data, raw_text


async def transition_to_analyzing_and_load(event_id: int) -> EventEnvelope | None:
    async with session_scope() as sess:
        stmt = (
            update(WebhookEvent)
            .where(WebhookEvent.id == event_id)
            .where(WebhookEvent.processing_status.in_(_ANALYZING_SOURCES))
            .values(
                processing_status=WebhookProcessingStatus.ANALYZING.value,
                failure_reason=None,
                error_message=None,
                next_retry_at=None,
            )
            .returning(WebhookEvent)
        )
        res = await sess.execute(stmt)
        event = res.scalar_one_or_none()
        if not event:
            return None
        headers = cast(dict[str, Any], event.headers or {})
        payload, raw_text = await load_event_payload(event)
        raw_body = raw_text.encode("utf-8") if raw_text else b""
        event_ts = event.timestamp.isoformat() if event.timestamp else None
        return EventEnvelope(
            headers=headers,
            payload=payload,
            raw_body=raw_body,
            source=event.source,
            event_ts=event_ts,
            request_id=event.request_id,
        )


async def list_recent_alert_contexts(alert_hash: str, now: datetime, window_minutes: int) -> list[AlertContext]:
    async with session_scope() as session:
        stmt = (
            select(WebhookEvent)
            .filter(
                WebhookEvent.timestamp >= now - timedelta(minutes=window_minutes),
                WebhookEvent.timestamp <= now,
                WebhookEvent.alert_hash != alert_hash,
            )
            .order_by(WebhookEvent.timestamp.desc())
            .limit(100)
        )
        res = await session.execute(stmt)
        return [
            AlertContext(
                e.id,
                e.source,
                normalize_importance(e.importance or "medium"),
                cast(dict[str, Any], e.parsed_data or {}),
                cast(dict[str, Any], e.ai_analysis or {}),
                e.timestamp or now,
                e.alert_hash,
            )
            for e in res.scalars().all()
        ]


def _snapshot_forward_rule(rule: ForwardRule) -> ForwardRuleSnapshot:
    raw = cast(dict[str, Any], rule.to_dict())
    return ForwardRuleSnapshot(
        id=rule.id,
        name=rule.name,
        match_importance=rule.match_importance,
        match_source=rule.match_source,
        match_duplicate=rule.match_duplicate,
        target_type=rule.target_type,
        target_url=rule.target_url,
        stop_on_match=rule.stop_on_match,
        extra=raw,
    )


async def list_enabled_forward_rules(session: Any | None = None) -> list[ForwardRuleSnapshot]:
    async def _list(sess: Any) -> list[ForwardRuleSnapshot]:
        stmt = select(ForwardRule).filter_by(enabled=True).order_by(ForwardRule.priority.desc())
        return [_snapshot_forward_rule(rule) for rule in (await sess.execute(stmt)).scalars().all()]

    if session is not None:
        return await _list(session)
    async with session_scope() as sess:
        return await _list(sess)


async def create_openclaw_analysis(
    webhook_event_id: int,
    *,
    run_id: str,
    session_key: str,
) -> int:
    from services.operations.taskiq_retry_scheduler import compute_openclaw_poll_delay, schedule_openclaw_poll

    initial_poll_delay = compute_openclaw_poll_delay(0)
    analysis_id: int
    async with session_scope() as sess:
        record = DeepAnalysis(
            webhook_event_id=webhook_event_id,
            engine="openclaw",
            openclaw_run_id=run_id,
            openclaw_session_key=session_key,
            status="pending",
            poll_attempts=0,
            next_poll_at=datetime.now() + timedelta(seconds=initial_poll_delay),
        )
        sess.add(record)
        await sess.flush()
        analysis_id = record.id
    with contextlib.suppress(Exception):
        await schedule_openclaw_poll(analysis_id, initial_poll_delay)
    return analysis_id


async def mark_last_notified(event_id: int) -> None:
    async with session_scope() as sess:
        await sess.execute(
            update(WebhookEvent).where(WebhookEvent.id == event_id).values(last_notified_at=datetime.now())
        )


async def mark_retry(
    event_id: int,
    *,
    max_retries: int,
    error_message: str,
    initial_delay: int,
    max_delay: int,
    multiplier: float,
) -> tuple[int, int] | None:
    from services.operations.taskiq_retry_scheduler import compute_backoff_delay

    async with session_scope() as sess:
        res = await sess.execute(
            update(WebhookEvent)
            .where(WebhookEvent.id == event_id)
            .where(WebhookEvent.retry_count < max_retries)
            .values(
                processing_status=WebhookProcessingStatus.RETRY.value,
                retry_count=WebhookEvent.retry_count + 1,
                failure_reason="retry_err",
                error_message=error_message[:2000],
            )
            .returning(WebhookEvent.retry_count)
        )
        row = res.first()
        if not row:
            return None
        retry_count = int(row[0] or 0)
        delay = compute_backoff_delay(
            retry_count,
            initial_delay=initial_delay,
            max_delay=max_delay,
            multiplier=multiplier,
        )
        await sess.execute(
            update(WebhookEvent)
            .where(WebhookEvent.id == event_id)
            .values(next_retry_at=datetime.now() + timedelta(seconds=delay))
        )
        return retry_count, delay


async def mark_dead_letter(event_id: int, *, retryable: bool, error_message: str) -> None:
    async with session_scope() as sess:
        await sess.execute(
            update(WebhookEvent)
            .where(WebhookEvent.id == event_id)
            .values(
                processing_status=WebhookProcessingStatus.DEAD_LETTER.value,
                failure_reason="retry_exhausted" if retryable else "fat_err",
                error_message=error_message[:2000],
                next_retry_at=None,
            )
        )

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

import orjson
from sqlalchemy import select

from core.compression import decompress_payload_async
from db.session import session_scope
from models import ForwardRule, WebhookEvent
from schemas import forward_rule_to_dict
from services.analysis.noise_reduction import AlertContext
from services.webhooks.decisioning import ForwardRuleSnapshot, normalize_importance


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
    raw = forward_rule_to_dict(rule)
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

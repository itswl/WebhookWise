from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core import json
from core.compression import decompress_payload_async
from db.session import count_with_timeout, session_scope
from core.datetime_utils import utcnow
from models import ForwardRule, SuppressedRecord, WebhookEvent
from services.analysis.noise_reduction import AlertContext
from services.webhooks.decisioning import ForwardRuleSnapshot, normalize_importance
from services.webhooks.types import AnalysisResult


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


async def check_duplicate_event(
    alert_hash: str,
    *,
    session: Any,
    time_window_hours: int = 24,
) -> DuplicateCheckResult:
    """Check duplicate state for one alert hash within a time window."""
    now = utcnow()
    threshold = now - timedelta(hours=time_window_hours)

    recent_stmt = (
        select(WebhookEvent)
        .filter(WebhookEvent.alert_hash == alert_hash, WebhookEvent.timestamp >= threshold)
        .order_by(WebhookEvent.timestamp.desc())
        .limit(1)
    )
    recent_res = await session.execute(recent_stmt)
    any_event = recent_res.scalar_one_or_none()

    if any_event:
        original_id = any_event.duplicate_of if any_event.is_duplicate else any_event.id
        original = await session.get(WebhookEvent, original_id) if original_id else any_event
        return DuplicateCheckResult(True, original or any_event)

    history_stmt = (
        select(WebhookEvent)
        .filter(WebhookEvent.alert_hash == alert_hash, WebhookEvent.is_duplicate.is_(False))
        .order_by(WebhookEvent.timestamp.desc())
        .limit(1)
    )
    history_res = await session.execute(history_stmt)
    history = history_res.scalar_one_or_none()

    if history:
        return DuplicateCheckResult(False, history)
    return DuplicateCheckResult(False, None)


async def load_event_payload(event: WebhookEvent) -> tuple[dict[str, Any] | None, str]:
    raw_text = await decompress_payload_async(event.raw_payload) or ""
    parsed_data = event.parsed_data
    if parsed_data is None and raw_text:
        try:
            loaded = json.loads(raw_text)
            parsed_data = loaded if isinstance(loaded, dict) else None
        except json.JSONDecodeError:
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
                cast(AnalysisResult, e.ai_analysis or {}),
                e.timestamp or now,
            )
            for e in res.scalars().all()
        ]


def _snapshot_forward_rule(rule: ForwardRule) -> ForwardRuleSnapshot:
    return ForwardRuleSnapshot(
        id=rule.id,
        name=rule.name,
        match_event_type=getattr(rule, "match_event_type", "") or "",
        match_importance=rule.match_importance,
        match_source=rule.match_source,
        match_duplicate=rule.match_duplicate,
        match_payload=getattr(rule, "match_payload", "") or "",
        target_type=rule.target_type,
        target_url=rule.target_url,
        stop_on_match=rule.stop_on_match,
        target_name=rule.target_name or "",
    )


async def list_enabled_forward_rules(session: Any | None = None) -> list[ForwardRuleSnapshot]:
    async def _list(sess: Any) -> list[ForwardRuleSnapshot]:
        stmt = select(ForwardRule).filter_by(enabled=True).order_by(ForwardRule.priority.desc())
        return [_snapshot_forward_rule(rule) for rule in (await sess.execute(stmt)).scalars().all()]

    if session is not None:
        return await _list(session)
    async with session_scope() as sess:
        return await _list(sess)


_rules_cache: list[ForwardRuleSnapshot] | None = None
_rules_cache_at: float = 0.0
_RULES_CACHE_TTL: float = 30.0


def invalidate_forward_rules_cache() -> None:
    global _rules_cache, _rules_cache_at
    _rules_cache = None
    _rules_cache_at = 0.0


async def get_cached_forward_rules(session: Any | None = None) -> list[ForwardRuleSnapshot]:
    import time

    global _rules_cache, _rules_cache_at
    now = time.monotonic()
    if _rules_cache is not None and (now - _rules_cache_at) < _RULES_CACHE_TTL:
        return _rules_cache
    rules = await list_enabled_forward_rules(session=session)
    _rules_cache = rules
    _rules_cache_at = now
    return rules


async def list_suppressed_records(
    session: AsyncSession,
    *,
    since_minutes: int = 60,
    limit: int = 100,
) -> list[dict[str, Any]]:
    since = utcnow() - timedelta(minutes=max(1, since_minutes))
    stmt = (
        select(SuppressedRecord)
        .where(SuppressedRecord.created_at >= since)
        .order_by(SuppressedRecord.created_at.desc(), SuppressedRecord.id.desc())
        .limit(max(1, min(500, limit)))
    )
    rows = (await session.execute(stmt)).scalars().all()
    items: list[dict[str, Any]] = [
        {
            "id": r.id,
            "alert_hash": r.alert_hash,
            "source": r.source,
            "relation": r.relation,
            "root_cause_event_id": r.root_cause_event_id,
            "reason": r.reason,
            "related_alert_ids": list(r.related_alert_ids or []),
            "confidence": r.confidence,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]
    return items


async def count_suppressed_records(session: AsyncSession, *, since_minutes: int = 60) -> int | None:
    since = utcnow() - timedelta(minutes=max(1, since_minutes))
    stmt = select(func.count()).select_from(SuppressedRecord).where(SuppressedRecord.created_at >= since)
    return await count_with_timeout(session, stmt)

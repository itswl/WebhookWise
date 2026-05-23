"""Webhook deduplication — Redis-first sliding window with DB fallback."""

import asyncio
import contextlib
import hashlib
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from core import json
from core.app_context import get_config_manager
from core.logger import get_logger
from core.observability.metrics import WEBHOOK_IDENTITY_DEGRADED_TOTAL, sanitize_source
from core.redis_client import redis_get_json_dict, redis_setex_json
from core.redis_health import webhook_dedupe
from db.session import session_scope

logger = get_logger("dedup")

# ── Dedup state (Redis operations) ───────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class DedupState:
    dedup_key: str
    original_event_id: int
    first_seen_at: float
    last_seen_at: float
    count: int
    analysis: dict[str, Any] | None

    @property
    def is_pending(self) -> bool:
        return self.original_event_id <= 0

    def is_active(self, now: float, window_seconds: int) -> bool:
        return (now - self.last_seen_at) <= window_seconds


def _dedup_state_key(dedup_key: str) -> str:
    return webhook_dedupe(dedup_key)


async def get_dedup_state(dedup_key: str) -> DedupState | None:
    try:
        payload = await redis_get_json_dict(_dedup_state_key(dedup_key))
    except Exception:
        return None
    if not payload:
        return None
    try:
        original_event_id = int(payload.get("original_event_id") or 0)
    except (TypeError, ValueError):
        return None

    analysis = payload.get("analysis")
    return DedupState(
        dedup_key=dedup_key,
        original_event_id=original_event_id,
        first_seen_at=float(payload.get("first_seen_at") or 0),
        last_seen_at=float(payload.get("last_seen_at") or 0),
        count=int(payload.get("count") or 1),
        analysis=analysis if isinstance(analysis, dict) else None,
    )


async def remember_dedup_state(
    dedup_key: str,
    original_event_id: int,
    analysis: dict[str, Any] | None,
    ttl_seconds: int,
    *,
    now: float | None = None,
) -> None:
    import time as _time

    current_time = now or _time.time()
    existing = await get_dedup_state(dedup_key)
    count = (existing.count + 1) if existing else 1
    first_seen_at = existing.first_seen_at if existing else current_time

    payload: dict[str, Any] = {
        "dedup_key": dedup_key,
        "original_event_id": original_event_id,
        "first_seen_at": first_seen_at,
        "last_seen_at": current_time,
        "count": count,
    }
    if analysis:
        payload["analysis"] = analysis
    with contextlib.suppress(Exception):
        await redis_setex_json(_dedup_state_key(dedup_key), max(60, ttl_seconds), payload)


# ── Dedup resolver ───────────────────────────────────────────────────────────


class DedupAction(StrEnum):
    NEW = "new"
    REUSE = "reuse"


@dataclass(frozen=True, slots=True)
class DedupResult:
    action: DedupAction
    analysis: dict[str, Any] | None
    original_event_id: int | None
    route_type: str = ""

    @property
    def is_duplicate(self) -> bool:
        return self.action == DedupAction.REUSE


def generate_event_keys(data: dict[str, Any], source: str) -> tuple[str, str]:
    """一次提取 identity 同时生成 alert_hash 和 dedup_key。"""
    from adapters.normalized import extract_alert_identity

    identity = extract_alert_identity(data)
    if identity:
        alert_key_fields: dict[str, object] = dict(identity)
        alert_key_fields.setdefault("source", source.strip().lower())
        alert_hash = hashlib.sha256(json.dumps_bytes(alert_key_fields, sort_keys=True)).hexdigest()

        dedup_key_fields: dict[str, object] = {}
        source_value = str(identity.get("source", source)).strip().lower()
        name_value = str(identity.get("name", "")).strip()
        if source_value:
            dedup_key_fields["source"] = source_value
        if name_value:
            dedup_key_fields["name"] = name_value
        resource = str(identity.get("resource", "") or "").strip()
        if resource:
            dedup_key_fields["resource"] = resource
        fingerprint = str(identity.get("fingerprint", "") or "").strip()
        if fingerprint:
            dedup_key_fields["fingerprint"] = fingerprint

        dedup_key = (
            hashlib.sha256(json.dumps_bytes(dedup_key_fields, sort_keys=True)).hexdigest()
            if dedup_key_fields
            else alert_hash
        )
        return alert_hash, dedup_key

    WEBHOOK_IDENTITY_DEGRADED_TOTAL.labels(sanitize_source(source)).inc()
    logger.debug("缺少 adapter 产出的告警 identity，使用完整 payload hash 兜底 source=%s", source)
    fallback_key_fields: dict[str, object] = {"source": source.strip().lower(), "payload": data}
    fallback_hash = hashlib.sha256(json.dumps_bytes(fallback_key_fields, sort_keys=True)).hexdigest()
    return fallback_hash, fallback_hash


def generate_alert_hash(data: dict, source: str) -> str:
    """Convenience wrapper — returns only the alert_hash portion of generate_event_keys."""
    return generate_event_keys(data, source)[0]


def _dedup_window_seconds() -> int:
    return int(get_config_manager().retry.DEDUP_WINDOW_SECONDS)


def _has_reusable_analysis(analysis: dict[str, Any] | None) -> bool:
    if not analysis:
        return False
    return not analysis.get("_degraded") and not analysis.get("_pending")


_PENDING_POLL_INTERVAL_S = 0.2
_PENDING_MAX_WAIT_S = 8.0


async def _poll_pending_state(dedup_key: str, window_seconds: int, deadline: float) -> DedupResult | None:
    while time.time() < deadline:
        await asyncio.sleep(_PENDING_POLL_INTERVAL_S)
        state = await get_dedup_state(dedup_key)
        if state and state.is_active(time.time(), window_seconds) and _has_reusable_analysis(state.analysis):
            logger.debug(
                "[Dedup] 轮询等待完成 dedup_key=%s orig_id=%s",
                dedup_key[:32] if dedup_key else "-",
                state.original_event_id,
            )
            return DedupResult(
                action=DedupAction.REUSE,
                analysis=state.analysis,
                original_event_id=state.original_event_id,
                route_type="redis_reuse",
            )
    return None


async def _find_original_by_dedup_key(dedup_key: str, window_seconds: int) -> dict[str, Any] | None:
    from datetime import datetime, timedelta

    from sqlalchemy import select

    from models import WebhookEvent

    now = datetime.now()
    threshold = now - timedelta(seconds=window_seconds)
    async with session_scope() as session:
        stmt = (
            select(WebhookEvent)
            .filter(
                WebhookEvent.dedup_key == dedup_key,
                WebhookEvent.timestamp >= threshold,
                WebhookEvent.is_duplicate.is_(False),
            )
            .order_by(WebhookEvent.timestamp.desc())
            .limit(1)
        )
        result = await session.execute(stmt)
        original = result.scalar_one_or_none()
        if original and _has_reusable_analysis(original.ai_analysis):
            return {"analysis": dict(original.ai_analysis or {}), "original_event_id": original.id}
    return None


async def resolve_dedup(dedup_key: str) -> DedupResult:
    window_seconds = _dedup_window_seconds()
    now = time.time()

    state = await get_dedup_state(dedup_key)
    if state and state.is_active(now, window_seconds):
        if _has_reusable_analysis(state.analysis):
            logger.debug(
                "[Dedup] Redis 滑动窗口命中 dedup_key=%s orig_id=%s count=%d",
                dedup_key[:32] if dedup_key else "-",
                state.original_event_id,
                state.count,
            )
            return DedupResult(
                action=DedupAction.REUSE,
                analysis=state.analysis,
                original_event_id=state.original_event_id,
                route_type="redis_reuse",
            )

        if state.is_pending:
            deadline = time.time() + _PENDING_MAX_WAIT_S
            logger.debug(
                "[Dedup] 发现进行中的去重状态，轮询等待 dedup_key=%s",
                dedup_key[:32] if dedup_key else "-",
            )
            poll_result = await _poll_pending_state(dedup_key, window_seconds, deadline)
            if poll_result:
                return poll_result

    db_result = await _find_original_by_dedup_key(dedup_key, window_seconds)
    if db_result:
        logger.debug(
            "[Dedup] DB fallback 命中 dedup_key=%s orig_id=%s",
            dedup_key[:32] if dedup_key else "-",
            db_result["original_event_id"],
        )
        return DedupResult(
            action=DedupAction.REUSE,
            analysis=db_result["analysis"],
            original_event_id=db_result["original_event_id"],
            route_type="db_reuse",
        )

    return DedupResult(
        action=DedupAction.NEW,
        analysis=None,
        original_event_id=None,
    )


__all__ = [
    "DedupResult",
    "DedupState",
    "generate_alert_hash",
    "generate_event_keys",
    "get_dedup_state",
    "remember_dedup_state",
    "resolve_dedup",
]

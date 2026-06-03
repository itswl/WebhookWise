"""Webhook deduplication — Redis-first sliding window with DB fallback."""

import hashlib
import time
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from redis.exceptions import RedisError

from core import json
from core.app_context import get_config_manager
from core.datetime_utils import utcnow
from core.logger import get_logger
from core.observability.metrics import REDIS_UNAVAILABLE_TOTAL
from core.redis_client import redis_get_json_dict, redis_setex_json
from core.redis_health import webhook_dedupe
from db.session import session_scope
from services.analysis.resource_risk import resource_dedup_bucket
from services.webhooks.types import is_analysis_degraded, is_pending_result

logger = get_logger("dedup")

# ── Dedup state (Redis operations) ───────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class DedupState:
    original_event_id: int
    first_seen_at: float
    last_seen_at: float
    count: int
    analysis: dict[str, Any] | None

    def is_active(self, now: float, window_seconds: int) -> bool:
        return (now - self.last_seen_at) <= window_seconds


async def get_dedup_state(dedup_key: str) -> DedupState | None:
    try:
        payload = await redis_get_json_dict(webhook_dedupe(dedup_key))
    except (RedisError, RuntimeError, TypeError, ValueError, json.JSONDecodeError) as e:
        REDIS_UNAVAILABLE_TOTAL.labels("dedup", "read_allowed").inc()
        logger.warning(
            "[Dedup] Redis 读取失败，继续走 DB fallback dedup_key=%s error_type=%s error=%s",
            dedup_key[:32] if dedup_key else "-",
            type(e).__name__,
            e,
            exc_info=True,
        )
        return None
    if not payload:
        return None
    try:
        original_event_id = int(payload.get("original_event_id") or 0)
    except (TypeError, ValueError):
        return None

    analysis = payload.get("analysis")
    return DedupState(
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
    reset_chain: bool = False,
) -> None:
    current_time = time.time()
    existing = await get_dedup_state(dedup_key)
    if reset_chain:
        count = 1
        first_seen_at = current_time
    else:
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
    try:
        await redis_setex_json(webhook_dedupe(dedup_key), max(60, ttl_seconds), payload)
    except (RedisError, RuntimeError, TypeError, ValueError) as e:
        REDIS_UNAVAILABLE_TOTAL.labels("dedup", "write_failed").inc()
        logger.warning(
            "[Dedup] Redis 写入失败，事件已继续处理但滑动窗口可能缺失 dedup_key=%s event_id=%s error_type=%s error=%s",
            dedup_key[:32] if dedup_key else "-",
            original_event_id,
            type(e).__name__,
            e,
            exc_info=True,
        )


# ── Dedup resolver ───────────────────────────────────────────────────────────


class DedupAction(StrEnum):
    NEW = "new"
    REUSE = "reuse"
    RECHAIN = "rechain"


@dataclass(frozen=True, slots=True)
class DedupResult:
    action: DedupAction
    analysis: dict[str, Any] | None
    original_event_id: int | None
    route_type: str = ""
    reset_chain: bool = False

    @property
    def is_duplicate(self) -> bool:
        return self.action == DedupAction.REUSE

    @property
    def is_rechain(self) -> bool:
        return self.action == DedupAction.RECHAIN


def generate_event_keys(data: Mapping[str, Any], source: str) -> tuple[str, str]:
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
        resource_bucket = resource_dedup_bucket(data)
        if resource_bucket:
            dedup_key_fields["resource_risk_bucket"] = resource_bucket

        dedup_key = (
            hashlib.sha256(json.dumps_bytes(dedup_key_fields, sort_keys=True)).hexdigest()
            if dedup_key_fields
            else alert_hash
        )
        return alert_hash, dedup_key

    logger.debug("缺少 adapter 产出的告警 identity，使用完整 payload hash 兜底 source=%s", source)
    fallback_key_fields: dict[str, object] = {"source": source.strip().lower(), "payload": data}
    fallback_hash = hashlib.sha256(json.dumps_bytes(fallback_key_fields, sort_keys=True)).hexdigest()
    return fallback_hash, fallback_hash


def generate_alert_hash(data: Mapping[str, Any], source: str) -> str:
    """Convenience wrapper — returns only the alert_hash portion of generate_event_keys."""
    return generate_event_keys(data, source)[0]


def _dedup_window_seconds() -> int:
    return int(get_config_manager().retry.DEDUP_WINDOW_SECONDS)


def _analysis_reuse_window_seconds() -> int:
    return int(get_config_manager().retry.ANALYSIS_REUSE_WINDOW_SECONDS)


def _has_reusable_analysis(analysis: dict[str, Any] | None) -> bool:
    if not analysis:
        return False
    return not is_analysis_degraded(analysis) and not is_pending_result(analysis)


async def _find_original_by_dedup_key(dedup_key: str, window_seconds: int) -> dict[str, Any] | None:
    from datetime import timedelta

    from sqlalchemy import select

    from models import WebhookEvent

    now = utcnow()
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
    dedup_window = _dedup_window_seconds()
    analysis_reuse_window = _analysis_reuse_window_seconds()
    now = time.time()
    reset_chain_on_new = False

    state = await get_dedup_state(dedup_key)
    if state:
        reset_chain_on_new = (now - state.last_seen_at) > dedup_window
    if state and _has_reusable_analysis(state.analysis):
        first_seen_elapsed = now - state.first_seen_at
        last_seen_elapsed = now - state.last_seen_at

        # RECHAIN: dedup 窗口过期但 AI 分析仍在复用窗口内 → 创建新告警但复用分析
        if (
            first_seen_elapsed > dedup_window
            and last_seen_elapsed <= dedup_window
            and first_seen_elapsed <= analysis_reuse_window
        ):
            logger.info(
                "[Dedup] 告警链超窗口，重建链 dedup_key=%s orig_id=%s first_seen_elapsed=%ds dedup_window=%ds analysis_window=%ds count=%d",
                dedup_key[:32] if dedup_key else "-",
                state.original_event_id,
                int(first_seen_elapsed),
                dedup_window,
                analysis_reuse_window,
                state.count,
            )
            return DedupResult(
                action=DedupAction.RECHAIN,
                analysis=state.analysis,
                original_event_id=state.original_event_id,
                route_type="rechain",
                reset_chain=True,
            )

        # REUSE: 常规去重窗口命中
        if state.is_active(now, dedup_window):
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

    db_result = await _find_original_by_dedup_key(dedup_key, dedup_window)
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

    if reset_chain_on_new:
        logger.info(
            "[Dedup] 过期链未命中 DB fallback，创建新链 dedup_key=%s previous_orig_id=%s",
            dedup_key[:32] if dedup_key else "-",
            state.original_event_id if state else "-",
        )

    return DedupResult(
        action=DedupAction.NEW,
        analysis=None,
        original_event_id=None,
        reset_chain=reset_chain_on_new,
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

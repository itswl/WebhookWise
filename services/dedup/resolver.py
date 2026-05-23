import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from core.app_context import get_config_manager
from core.logger import get_logger
from db.session import session_scope
from services.dedup.state import get_dedup_state

logger = get_logger("dedup.resolver")


class DedupAction(StrEnum):
    NEW = "new"
    REUSE = "reuse"


@dataclass(frozen=True, slots=True)
class DedupResult:
    action: DedupAction
    analysis: dict[str, Any] | None
    original_event_id: int | None
    is_reused: bool
    route_type: str = ""

    @property
    def is_duplicate(self) -> bool:
        return self.action == DedupAction.REUSE


def _dedup_window_seconds() -> int:
    return int(get_config_manager().retry.DEDUP_WINDOW_SECONDS)


def _ttl_seconds() -> int:
    return max(60, _dedup_window_seconds() * 2)


def _has_reusable_analysis(analysis: dict[str, Any] | None) -> bool:
    if not analysis:
        return False
    return not analysis.get("_degraded")


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
            return {"analysis": dict(original.ai_analysis), "original_event_id": original.id}
    return None


async def resolve_dedup(dedup_key: str) -> DedupResult:
    window_seconds = _dedup_window_seconds()
    now = time.time()

    state = await get_dedup_state(dedup_key)
    if state and state.is_active(now, window_seconds) and _has_reusable_analysis(state.analysis):
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
            is_reused=True,
            route_type="redis_reuse",
        )

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
            is_reused=False,
            route_type="db_reuse",
        )

    return DedupResult(
        action=DedupAction.NEW,
        analysis=None,
        original_event_id=None,
        is_reused=False,
    )

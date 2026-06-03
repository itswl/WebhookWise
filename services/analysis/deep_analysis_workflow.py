from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from contracts.webhook_payload import JsonObject, WebhookData
from core.datetime_utils import utc_isoformat, utcnow
from core.logger import get_logger
from models import DeepAnalysis, WebhookEvent
from services.analysis.ai_analyzer import analyze_webhook_with_ai
from services.webhooks.types import (
    MANUAL_RETRY_STARTED_AT,
    AnalysisResult,
    DeepAnalysisStatus,
    ForwardResult,
    analysis_degraded_reason,
    is_analysis_degraded,
)

logger = get_logger("analysis.deep_analysis_workflow")
MANUAL_RETRY_STARTED_AT_KEY = MANUAL_RETRY_STARTED_AT


def is_supported_deep_analysis_engine(requested: str) -> bool:
    return requested in ("", "auto", "openclaw")


async def run_openclaw_deep_analysis(
    ctx: Mapping[str, Any], headers: dict[str, Any], user_question: str
) -> tuple[AnalysisResult | ForwardResult, str]:
    from services.analysis.openclaw import analyze_with_openclaw

    webhook_data: WebhookData = {
        "source": str(ctx["source"]),
        "headers": headers,
        "parsed_data": dict(ctx["parsed_data"]) if isinstance(ctx.get("parsed_data"), dict) else {},
    }
    result = await analyze_with_openclaw(webhook_data, user_question)
    if is_analysis_degraded(result):
        logger.warning("[DeepAnalysis] OpenClaw 降级，回退本地 AI: %s", analysis_degraded_reason(result))
        return await analyze_webhook_with_ai(webhook_data), "local (fallback)"
    return result, "openclaw"


async def notify_completed_deep_analysis(session: AsyncSession, record: DeepAnalysis) -> None:
    from services.operations.deep_analysis_notifications import (
        EVENT_IMPORTANCE_KEY,
        EVENT_IS_DUPLICATE_KEY,
        EVENT_PARSED_DATA_KEY,
        send_deep_analysis_success_notification,
    )

    event = await session.get(WebhookEvent, record.webhook_event_id)
    source = event.source if event else ""
    record_dict: JsonObject = {
        "id": record.id,
        "webhook_event_id": record.webhook_event_id,
        "engine": record.engine,
        "analysis_result": record.analysis_result,
        "duration_seconds": record.duration_seconds,
    }
    if event:
        record_dict[EVENT_IMPORTANCE_KEY] = str(getattr(event, "importance", "") or "")
        record_dict[EVENT_IS_DUPLICATE_KEY] = bool(getattr(event, "is_duplicate", False))
        parsed_data = getattr(event, "parsed_data", None)
        record_dict[EVENT_PARSED_DATA_KEY] = dict(parsed_data or {}) if isinstance(parsed_data, dict) else {}
    await send_deep_analysis_success_notification(record_dict, source)


async def notify_completed_deep_analysis_best_effort(session: AsyncSession, record: DeepAnalysis) -> None:
    try:
        await notify_completed_deep_analysis(session, record)
    except Exception as e:
        logger.error(
            "[DeepAnalysis] 完成通知发送失败 analysis_id=%s webhook_id=%s error=%s",
            record.id,
            record.webhook_event_id,
            e,
            exc_info=True,
        )


async def clear_openclaw_poll_state_best_effort(analysis_id: int) -> None:
    from services.analysis.openclaw import clear_openclaw_poll_state

    try:
        await clear_openclaw_poll_state(analysis_id)
    except Exception as e:
        logger.error(
            "[DeepAnalysis] 清理 OpenClaw poll 状态失败 analysis_id=%s error=%s", analysis_id, e, exc_info=True
        )


def prepare_openclaw_poll_if_pending(record: DeepAnalysis) -> int | None:
    if record.status != DeepAnalysisStatus.PENDING:
        return None
    from services.operations.taskiq_retry_scheduler import compute_openclaw_poll_delay

    delay = compute_openclaw_poll_delay(record.poll_attempts or 0)
    record.next_poll_at = utcnow() + timedelta(seconds=delay)
    return delay


def reset_deep_analysis_for_background_poll(record: DeepAnalysis, now: datetime) -> None:
    record.status = DeepAnalysisStatus.PENDING
    record.analysis_result = {MANUAL_RETRY_STARTED_AT_KEY: utc_isoformat(now)}
    record.duration_seconds = 0
    record.poll_attempts = 0
    record.last_polled_at = None
    record.next_poll_at = now

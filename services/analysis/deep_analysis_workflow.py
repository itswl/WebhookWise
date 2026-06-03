from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from contracts.webhook_payload import JsonObject, WebhookData
from core.datetime_utils import utc_isoformat, utcnow
from core.logger import get_logger, mask_url
from core.url_security import UnsafeTargetUrlError, validate_outbound_url
from models import DeepAnalysis, WebhookEvent
from services.analysis.ai_analyzer import analyze_webhook_with_ai
from services.operations import taskiq_retry_scheduler
from services.webhooks.event_context import build_webhook_context
from services.webhooks.types import (
    MANUAL_RETRY_STARTED_AT,
    AnalysisResult,
    DeepAnalysisStatus,
    ForwardResult,
    analysis_degraded_reason,
    is_analysis_degraded,
    is_pending_result,
    openclaw_run_id,
    openclaw_session_key,
)

logger = get_logger("analysis.deep_analysis_workflow")
MANUAL_RETRY_STARTED_AT_KEY = MANUAL_RETRY_STARTED_AT
RETRYABLE_DEEP_ANALYSIS_STATUSES = frozenset(
    {
        DeepAnalysisStatus.FAILED,
        DeepAnalysisStatus.COMPLETED,
        DeepAnalysisStatus.PENDING,
        DeepAnalysisStatus.TIMEOUT,
        DeepAnalysisStatus.DEGRADED,
        DeepAnalysisStatus.ERROR,
    }
)
_BEST_EFFORT_ERRORS = (OSError, RuntimeError, SQLAlchemyError, TimeoutError, ValueError)


class DeepAnalysisWorkflowError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class DeepAnalysisDeliveryError(RuntimeError):
    pass


class DeepAnalysisExecutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class DeepAnalysisRetryOutcome:
    message: str
    record: DeepAnalysis | None = None


@dataclass(frozen=True)
class DeepAnalysisForwardOutcome:
    outbox_id: object


def is_supported_deep_analysis_engine(requested: str) -> bool:
    return requested in ("", "auto", "openclaw")


async def build_deep_analysis_context(event: WebhookEvent) -> JsonObject:
    return await build_webhook_context(event)


async def run_openclaw_deep_analysis(
    ctx: Mapping[str, Any], headers: dict[str, Any], user_question: str
) -> tuple[AnalysisResult | ForwardResult, str]:
    from services.analysis.openclaw_analysis import analyze_with_openclaw

    webhook_data: WebhookData = {
        "source": str(ctx["source"]),
        "headers": headers,
        "parsed_data": dict(ctx["parsed_data"]) if isinstance(ctx.get("parsed_data"), dict) else {},
    }
    try:
        result = await analyze_with_openclaw(webhook_data, user_question)
    except (OSError, RuntimeError, TimeoutError, ValueError) as e:
        raise DeepAnalysisExecutionError("OpenClaw analysis failed") from e
    if is_analysis_degraded(result):
        logger.warning("[DeepAnalysis] OpenClaw 降级，回退本地 AI: %s", analysis_degraded_reason(result))
        try:
            return await analyze_webhook_with_ai(webhook_data), "local (fallback)"
        except (OSError, RuntimeError, TimeoutError, ValueError) as e:
            raise DeepAnalysisExecutionError("Fallback analysis failed") from e
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
    except _BEST_EFFORT_ERRORS as e:
        logger.error(
            "[DeepAnalysis] 完成通知发送失败 analysis_id=%s webhook_id=%s error=%s",
            record.id,
            record.webhook_event_id,
            e,
            exc_info=True,
        )


async def clear_openclaw_poll_state_best_effort(analysis_id: int) -> None:
    from services.analysis.openclaw_poll import clear_openclaw_poll_state

    try:
        await clear_openclaw_poll_state(analysis_id)
    except _BEST_EFFORT_ERRORS as e:
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


async def retry_deep_analysis_record(session: AsyncSession, analysis_id: int) -> DeepAnalysisRetryOutcome:
    logger.info("[DeepAnalysis] 重试请求 analysis_id=%s", analysis_id)
    record = await session.get(DeepAnalysis, analysis_id)
    if not record:
        logger.warning("[DeepAnalysis] 重试失败，记录不存在 analysis_id=%s", analysis_id)
        raise DeepAnalysisWorkflowError("分析记录不存在", status_code=404)

    if record.status not in RETRYABLE_DEEP_ANALYSIS_STATUSES:
        logger.warning("[DeepAnalysis] 重试失败，状态不可重试 analysis_id=%s status=%s", analysis_id, record.status)
        raise DeepAnalysisWorkflowError(f"当前状态不可重试: {record.status}", status_code=400)

    if not record.openclaw_session_key:
        event = await session.get(WebhookEvent, record.webhook_event_id)
        if not event:
            logger.warning(
                "[DeepAnalysis] 重试失败，关联 webhook 不存在 analysis_id=%s webhook_id=%s",
                analysis_id,
                record.webhook_event_id,
            )
            raise DeepAnalysisWorkflowError("关联的 webhook 事件不存在", status_code=404)

        ctx = await build_deep_analysis_context(event)
        new_result, engine_name = await run_openclaw_deep_analysis(ctx, event.headers or {}, record.user_question or "")
        if is_pending_result(new_result):
            now = utcnow()
            record.status = DeepAnalysisStatus.PENDING
            record.analysis_result = {**new_result, MANUAL_RETRY_STARTED_AT_KEY: utc_isoformat(now)}
            record.openclaw_run_id = openclaw_run_id(new_result)
            record.openclaw_session_key = openclaw_session_key(new_result)
            record.duration_seconds = 0
            record.poll_attempts = 0
            record.last_polled_at = None
            await session.flush()
            poll_delay = prepare_openclaw_poll_if_pending(record)
            await session.commit()
            if poll_delay is not None:
                await taskiq_retry_scheduler.schedule_openclaw_poll_best_effort(record.id, poll_delay)
            logger.info("[DeepAnalysis] 已重新发起后台分析 analysis_id=%s poll_delay=%s", record.id, poll_delay)
            return DeepAnalysisRetryOutcome(message="已重新发起分析任务，请等待结果")

        record.status = DeepAnalysisStatus.COMPLETED
        record.engine = engine_name
        record.analysis_result = dict(new_result)
        record.duration_seconds = 0
        await session.flush()
        await notify_completed_deep_analysis_best_effort(session, record)
        await session.commit()
        logger.info("[DeepAnalysis] 重试后同步完成 analysis_id=%s engine=%s", record.id, engine_name)
        return DeepAnalysisRetryOutcome(message="分析已完成")

    reset_deep_analysis_for_background_poll(record, utcnow())
    await session.flush()
    await session.commit()
    await clear_openclaw_poll_state_best_effort(int(record.id))
    await taskiq_retry_scheduler.schedule_openclaw_poll_best_effort(int(record.id), 0)
    logger.info("[DeepAnalysis] 已提交后台拉取 analysis_id=%s webhook_id=%s", record.id, record.webhook_event_id)
    return DeepAnalysisRetryOutcome(message="已提交后台拉取，请稍后刷新查看结果", record=record)


async def forward_deep_analysis_record(
    session: AsyncSession,
    analysis_id: int,
    target_url: str,
) -> DeepAnalysisForwardOutcome:
    logger.info("[DeepAnalysis] 手动转发请求 analysis_id=%s target=%s", analysis_id, mask_url(target_url))
    if not target_url:
        raise DeepAnalysisWorkflowError("转发 URL 不能为空", status_code=400)
    if not target_url.startswith(("http://", "https://")):
        raise DeepAnalysisWorkflowError("URL 格式无效", status_code=400)
    try:
        target_url = await validate_outbound_url(target_url)
    except UnsafeTargetUrlError as e:
        logger.warning("[DeepAnalysis] 手动转发目标 URL 被拒绝 analysis_id=%s error=%s", analysis_id, e)
        raise

    analysis = await session.get(DeepAnalysis, analysis_id)
    if not analysis:
        logger.warning("[DeepAnalysis] 手动转发失败，记录不存在 analysis_id=%s", analysis_id)
        raise DeepAnalysisWorkflowError("分析记录不存在", status_code=404)
    if analysis.status != DeepAnalysisStatus.COMPLETED:
        logger.warning("[DeepAnalysis] 手动转发失败，分析未完成 analysis_id=%s status=%s", analysis_id, analysis.status)
        raise DeepAnalysisWorkflowError("分析尚未完成", status_code=400)

    source = "unknown"
    if analysis.webhook_event_id:
        event = await session.get(WebhookEvent, analysis.webhook_event_id)
        if event:
            source = event.source or "unknown"

    from services.forwarding.outbox import forward_notification
    from services.notifications.feishu import build_deep_analysis_card, is_feishu_url

    fwd_payload: dict[str, Any] = {
        "type": "deep_analysis",
        "analysis_id": analysis_id,
        "source": source,
        "engine": analysis.engine,
        "webhook_event_id": analysis.webhook_event_id,
        "analysis_result": analysis.analysis_result,
        "duration_seconds": analysis.duration_seconds,
        "created_at": utc_isoformat(analysis.created_at),
    }
    formatted_payload: JsonObject = (
        build_deep_analysis_card(
            {
                "analysis_result": analysis.analysis_result,
                "engine": analysis.engine,
                "duration_seconds": analysis.duration_seconds,
            },
            source=source,
            webhook_event_id=analysis.webhook_event_id or 0,
        )
        if is_feishu_url(target_url)
        else fwd_payload
    )
    try:
        result = await forward_notification(
            event_type="deep_analysis_manual",
            source=source,
            formatted_payload=formatted_payload,
            webhook_id=analysis.webhook_event_id or None,
            target_url=target_url,
            idempotency_extra=f"manual-deep-analysis:{analysis_id}:{uuid4().hex}",
        )
    except _BEST_EFFORT_ERRORS as e:
        raise DeepAnalysisDeliveryError("Failed to enqueue deep analysis forward") from e

    outbox_id = result.get("outbox_id")
    status = result.get("status", "")
    if status == "skipped":
        reason = result.get("reason", "未知")
        logger.warning(
            "[DeepAnalysis] 手动转发被跳过 analysis_id=%s target=%s reason=%s",
            analysis_id,
            mask_url(target_url),
            reason,
        )
        raise DeepAnalysisWorkflowError("转发未送达", status_code=400)
    logger.info(
        "[DeepAnalysis] 手动转发已入队 analysis_id=%s webhook_id=%s outbox_id=%s target=%s",
        analysis_id,
        analysis.webhook_event_id,
        outbox_id,
        mask_url(target_url),
    )
    return DeepAnalysisForwardOutcome(outbox_id=outbox_id)

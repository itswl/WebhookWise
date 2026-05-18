"""Failed-forward retry execution.

DB stores audit state; TaskIQ's dynamic scheduler owns retry timing. This keeps
PostgreSQL out of the message-queue role and avoids periodic due-row scans.
"""

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from core.logger import mask_url
from core.metrics import FORWARD_RETRY_TOTAL
from core.otel import span as otel_span
from db.session import session_scope
from models import FailedForward, WebhookEvent
from services.forwarding.policies import ForwardRetryPolicy
from services.webhooks.types import FailedForwardStatus

logger = logging.getLogger("webhook_service.forward_retry")


async def retry_failed_forward_by_id(failed_forward_id: int, *, retry_policy: ForwardRetryPolicy | None = None) -> None:
    """Execute one failed-forward retry scheduled by TaskIQ."""
    policy = retry_policy or ForwardRetryPolicy.from_config()
    try:
        async with session_scope() as session:
            stmt = (
                select(FailedForward)
                .options(defer(FailedForward.forward_data), defer(FailedForward.forward_headers))
                .where(FailedForward.id == failed_forward_id)
            )
            result = await session.execute(stmt)
            record = result.scalar_one_or_none()
            if not record:
                return
            if record.status not in (FailedForwardStatus.PENDING, FailedForwardStatus.RETRYING):
                return
            with otel_span(
                "forward.retry",
                {
                    "event_id": record.webhook_event_id,
                    "retry_count": record.retry_count + 1,
                    "forward.status": str(record.status),
                },
            ):
                await _retry_forward(session, record, retry_policy=policy)
    except Exception as e:
        logger.error("[ForwardRetry] 重试记录 ID=%s 异常: %s", failed_forward_id, e)


async def schedule_failed_forward_many(failed_forward_ids: list[int]) -> None:
    """Best-effort immediate dispatch for due failed-forward retry rows."""
    if not failed_forward_ids:
        return

    from services.operations.tasks import retry_failed_forward_task

    for failed_forward_id in failed_forward_ids:
        try:
            await retry_failed_forward_task.kiq(failed_forward_id=failed_forward_id)
        except Exception as e:  # noqa: PERF203
            logger.warning("[ForwardRetry] 即时调度失败 ID=%s error=%s，将由扫描任务兜底", failed_forward_id, e)


async def run_failed_forward_scan(limit: int = 100) -> int:
    """Queue due FailedForward records when dynamic schedules were missed."""
    now = datetime.now()
    async with session_scope() as session:
        stmt = (
            select(FailedForward.id)
            .where(FailedForward.status.in_([FailedForwardStatus.PENDING, FailedForwardStatus.RETRYING]))
            .where((FailedForward.next_retry_at.is_(None)) | (FailedForward.next_retry_at <= now))
            .order_by(FailedForward.next_retry_at.asc(), FailedForward.id.asc())
            .limit(limit)
        )
        ids = list((await session.execute(stmt)).scalars().all())
    await schedule_failed_forward_many(ids)
    return len(ids)


async def _retry_forward(
    session: AsyncSession, record: FailedForward, *, retry_policy: ForwardRetryPolicy | None = None
) -> None:
    """重试单条转发失败记录"""
    from services.forwarding.forward import forward_to_remote

    policy = retry_policy or ForwardRetryPolicy.from_config()

    logger.info(
        "[ForwardRetry] 开始重试: ID=%s target=%s attempt=%d/%d",
        record.id,
        mask_url(record.target_url),
        record.retry_count + 1,
        record.max_retries,
    )

    # 从 DB 获取关联的 WebhookEvent（获取 ai_analysis 等信息）
    event = await session.get(WebhookEvent, record.webhook_event_id)
    if not event:
        logger.warning("[ForwardRetry] 关联事件不存在: webhook_event_id=%s, 标记为 exhausted", record.webhook_event_id)
        record.status = FailedForwardStatus.EXHAUSTED
        record.updated_at = datetime.now()
        await session.flush()
        return

    # 构建 webhook_data 和 analysis_result
    webhook_data: dict[str, Any] = {
        "parsed_data": event.parsed_data or {},
        "source": event.source,
        "timestamp": event.timestamp.isoformat() if event.timestamp else None,
        "client_ip": event.client_ip,
    }
    analysis_result: dict[str, Any] = dict(event.ai_analysis or {})

    now = datetime.now()

    try:
        result = await forward_to_remote(
            webhook_data=webhook_data,
            analysis_result=analysis_result,
            target_url=record.target_url,
        )

        # 判断转发结果
        status = result.get("status", "")
        if status in ("success", "disabled"):
            record.status = FailedForwardStatus.SUCCESS
            record.last_retry_at = now
            record.updated_at = now
            FORWARD_RETRY_TOTAL.labels(status="success").inc()
            logger.info("[ForwardRetry] 重试成功: ID=%s, webhook_event_id=%s", record.id, record.webhook_event_id)
        else:
            await _handle_retry_failure(
                record, now, f"forward status={status}: {result.get('message', '')}", retry_policy=policy
            )

    except Exception as e:
        await _handle_retry_failure(record, now, str(e), retry_policy=policy)

    await session.flush()


async def _handle_retry_failure(
    record: FailedForward, now: datetime, error_msg: str, *, retry_policy: ForwardRetryPolicy | None = None
) -> None:
    """处理重试失败：更新计数、计算下次重试时间或标记为 exhausted"""
    policy = retry_policy or ForwardRetryPolicy.from_config()
    record.retry_count += 1
    record.last_retry_at = now
    record.error_message = error_msg
    record.updated_at = now

    if record.retry_count >= record.max_retries:
        record.status = FailedForwardStatus.EXHAUSTED
        FORWARD_RETRY_TOTAL.labels(status="exhausted").inc()
        logger.warning(
            "[ForwardRetry] 重试次数已耗尽: ID=%s, retry_count=%d/%d",
            record.id,
            record.retry_count,
            record.max_retries,
        )
    else:
        record.status = FailedForwardStatus.RETRYING
        FORWARD_RETRY_TOTAL.labels(status="failed").inc()
        delay = policy.delay_for_attempt(record.retry_count)
        record.next_retry_at = now + timedelta(seconds=delay)
        try:
            from services.operations.taskiq_retry_scheduler import schedule_forward_retry

            await schedule_forward_retry(record.id, int(delay))
        except Exception as e:
            logger.warning("[ForwardRetry] TaskIQ 重试调度失败 ID=%s error=%s", record.id, e)
        logger.info(
            "[ForwardRetry] 记录 ID=%s 将在 %.0fs 后重试 (retry_count=%d/%d)",
            record.id,
            delay,
            record.retry_count,
            record.max_retries,
        )

"""
转发失败重试 Poller — 后台指数退避重试

TaskIQ Scheduler 周期性触发本任务，Worker 从 DB 中扫描到期的
failed_forward 记录并执行重试。DB 是失败转发重试状态与审计的唯一来源；
Redis 只保留 TaskIQ 队列、锁和缓存职责。
"""

import asyncio
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from core.config import Config
from core.distributed_lock import DistributedLock
from core.metrics import FORWARD_RETRY_TOTAL
from db.session import session_scope
from models import FailedForward, WebhookEvent

logger = logging.getLogger("webhook_service.forward_retry")


async def poll_pending_retries() -> None:
    """核心逻辑：获取分布式锁，扫描 DB 中已到期的转发重试记录。"""
    lock_key = "forward:retry:poller:lock"

    lock_ttl = max(
        60,
        int(
            (Config.retry.FORWARD_RETRY_BATCH_SIZE / max(1, Config.retry.FORWARD_RETRY_CONCURRENCY))
            * max(1, Config.server.FORWARD_REQUEST_TIMEOUT_SECONDS)
            + 60
        ),
    )
    lock_ttl = min(lock_ttl, 600)

    lock_value = str(uuid.uuid4())
    lock = DistributedLock(key=lock_key, ttl=lock_ttl, lock_value=lock_value)
    async with lock as acquired:
        if not acquired:
            return
        async with session_scope() as session:
            record_ids = await _load_due_retry_ids(session, limit=Config.retry.FORWARD_RETRY_BATCH_SIZE)
        if not record_ids:
            return

        logger.info("[ForwardRetry] 本轮 DB 到期重试 %d 条", len(record_ids))

        semaphore = asyncio.Semaphore(max(1, Config.retry.FORWARD_RETRY_CONCURRENCY))

        async def _retry_one(record_id: int) -> None:
            async with semaphore:
                try:
                    async with session_scope() as inner_session:
                        stmt = (
                            select(FailedForward)
                            .options(defer(FailedForward.forward_data), defer(FailedForward.forward_headers))
                            .where(FailedForward.id == record_id)
                        )
                        result = await inner_session.execute(stmt)
                        record = result.scalar_one_or_none()
                        if not record:
                            return
                        if record.status not in ("pending", "retrying"):
                            return
                        if record.next_retry_at and record.next_retry_at > datetime.now():
                            return
                        await _retry_forward(inner_session, record)
                except Exception as e:  # noqa: PERF203
                    logger.error(f"[ForwardRetry] 重试记录 ID={record_id} 异常: {e}")

        await asyncio.gather(*[_retry_one(rid) for rid in record_ids])


async def _load_due_retry_ids(session: AsyncSession, *, limit: int, now: datetime | None = None) -> list[int]:
    """Return due failed_forward IDs ordered by retry time.

    The Redis distributed lock around the poller keeps concurrent workers from
    double-scanning. The DB query remains the durable source of retry timing.
    """
    due_at = now or datetime.now()
    stmt = (
        select(FailedForward.id)
        .where(FailedForward.status.in_(("pending", "retrying")))
        .where(FailedForward.next_retry_at.is_not(None))
        .where(FailedForward.next_retry_at <= due_at)
        .order_by(FailedForward.next_retry_at.asc(), FailedForward.id.asc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def _retry_forward(session: AsyncSession, record: FailedForward) -> None:
    """重试单条转发失败记录"""
    from services.forwarding.forward import forward_to_remote

    logger.info(
        "[ForwardRetry] 开始重试: ID=%s target=%s attempt=%d/%d",
        record.id,
        record.target_url,
        record.retry_count + 1,
        record.max_retries,
    )

    # 从 DB 获取关联的 WebhookEvent（获取 ai_analysis 等信息）
    event = await session.get(WebhookEvent, record.webhook_event_id)
    if not event:
        logger.warning(f"[ForwardRetry] 关联事件不存在: webhook_event_id={record.webhook_event_id}, 标记为 exhausted")
        record.status = "exhausted"
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
            record.status = "success"
            record.last_retry_at = now
            record.updated_at = now
            FORWARD_RETRY_TOTAL.labels(status="success").inc()
            logger.info(f"[ForwardRetry] 重试成功: ID={record.id}, webhook_event_id={record.webhook_event_id}")
        else:
            await _handle_retry_failure(record, now, f"forward status={status}: {result.get('message', '')}")

    except Exception as e:
        await _handle_retry_failure(record, now, str(e))

    await session.flush()


async def _handle_retry_failure(record: FailedForward, now: datetime, error_msg: str) -> None:
    """处理重试失败：更新计数、计算下次重试时间或标记为 exhausted"""
    record.retry_count += 1
    record.last_retry_at = now
    record.error_message = error_msg
    record.updated_at = now

    if record.retry_count >= record.max_retries:
        record.status = "exhausted"
        FORWARD_RETRY_TOTAL.labels(status="exhausted").inc()
        logger.warning(
            f"[ForwardRetry] 重试次数已耗尽: ID={record.id}, retry_count={record.retry_count}/{record.max_retries}"
        )
    else:
        record.status = "retrying"
        FORWARD_RETRY_TOTAL.labels(status="failed").inc()
        # 指数退避：min(initial_delay * multiplier^(retry_count-1), max_delay)
        delay = min(
            Config.retry.FORWARD_RETRY_INITIAL_DELAY
            * Config.retry.FORWARD_RETRY_BACKOFF_MULTIPLIER ** (record.retry_count - 1),
            Config.retry.FORWARD_RETRY_MAX_DELAY,
        )
        record.next_retry_at = now + timedelta(seconds=delay)
        logger.info(
            f"[ForwardRetry] 记录 ID={record.id} 将在 {delay:.0f}s 后重试 "
            f"(retry_count={record.retry_count}/{record.max_retries})"
        )

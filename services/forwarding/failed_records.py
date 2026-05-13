"""Failed-forward audit records and manual retry reset."""

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any, TypeVar

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.logger import logger, mask_url
from db.session import count_with_timeout, session_scope
from models import FailedForward
from services.forwarding.policies import ForwardRetryPolicy
from services.webhooks.types import FailedForwardStatus

_T = TypeVar("_T")


async def _with_session(
    session: AsyncSession | None, fn: Callable[..., Awaitable[_T]], *args: Any, **kwargs: Any
) -> _T:
    """Run *fn* with either a provided session or a newly scoped one."""
    if session is not None:
        return await fn(session, *args, **kwargs)
    async with session_scope() as scoped:
        return await fn(scoped, *args, **kwargs)


async def _schedule_failed_forward_retry(
    record_id: int, delay_seconds: int, *, policy: ForwardRetryPolicy | None = None
) -> None:
    policy = policy or ForwardRetryPolicy.from_config()
    if not policy.enabled:
        return
    try:
        from services.operations.taskiq_retry_scheduler import schedule_forward_retry

        await schedule_forward_retry(record_id, delay_seconds)
    except Exception as e:
        logger.warning("[ForwardRetry] TaskIQ 重试调度失败 record_id=%s error=%s", record_id, e)


async def record_failed_forward(
    webhook_event_id: int,
    forward_rule_id: int | None,
    target_url: str,
    target_type: str,
    failure_reason: str,
    error_message: str | None = None,
    forward_data: dict[str, Any] | None = None,
    forward_headers: dict[str, Any] | None = None,
    max_retries: int | None = None,
    session: AsyncSession | None = None,
    retry_policy: ForwardRetryPolicy | None = None,
) -> FailedForward | None:
    """写入转发失败记录，计算首次重试时间"""
    retry_policy = retry_policy or ForwardRetryPolicy.from_config()
    if max_retries is None:
        max_retries = retry_policy.max_retries

    now = datetime.now()
    next_retry_at = now + timedelta(seconds=retry_policy.initial_delay)

    record = FailedForward(
        webhook_event_id=webhook_event_id,
        forward_rule_id=forward_rule_id,
        target_url=target_url,
        target_type=target_type,
        status=FailedForwardStatus.PENDING,
        failure_reason=failure_reason,
        error_message=error_message,
        retry_count=0,
        max_retries=max_retries,
        next_retry_at=next_retry_at,
        forward_data=forward_data,
        forward_headers=forward_headers,
        created_at=now,
        updated_at=now,
    )

    async def _persist(sess: AsyncSession) -> FailedForward:
        sess.add(record)
        await sess.flush()
        await _schedule_failed_forward_retry(record.id, retry_policy.initial_delay, policy=retry_policy)
        return record

    try:
        persisted = await _with_session(session, _persist)
        logger.info(
            "[ForwardRetry] 转发失败记录已写入 id=%s event_id=%s target=%s status=%s",
            persisted.id,
            webhook_event_id,
            mask_url(target_url),
            persisted.status,
        )
        return persisted
    except Exception as e:
        logger.error(
            "[ForwardRetry] 写入转发失败记录失败 event_id=%s target=%s error=%s",
            webhook_event_id,
            mask_url(target_url),
            e,
        )
        return None


async def get_failed_forwards(
    status: str | None = None,
    target_type: str | None = None,
    limit: int = 20,
    offset: int = 0,
    session: AsyncSession | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """按状态/类型分页查询转发失败记录"""

    async def _query(sess: AsyncSession) -> tuple[list[dict[str, Any]], int]:
        conditions = []
        if status:
            conditions.append(FailedForward.status == status)
        if target_type:
            conditions.append(FailedForward.target_type == target_type)

        count_stmt = select(func.count()).select_from(FailedForward)
        for cond in conditions:
            count_stmt = count_stmt.filter(cond)
        total = await count_with_timeout(sess, count_stmt) or 0

        query = select(FailedForward)
        for cond in conditions:
            query = query.filter(cond)
        query = query.order_by(FailedForward.next_retry_at.asc()).offset(offset).limit(limit)
        result = await sess.execute(query)
        records = result.scalars().all()
        return [r.to_dict() for r in records], total

    return await _with_session(session, _query)


async def get_failed_forward_stats(session: AsyncSession | None = None) -> dict[str, int]:
    async def _query(sess: AsyncSession) -> dict[str, int]:
        stmt = select(FailedForward.status, func.count()).group_by(FailedForward.status)
        result = await sess.execute(stmt)
        rows = result.all()
        stats = {"pending": 0, "retrying": 0, "success": 0, "exhausted": 0, "total": 0}
        for status_val, count in rows:
            if status_val in stats:
                stats[status_val] = count
            stats["total"] += count
        return stats

    return await _with_session(session, _query)


async def manual_retry_reset(
    failed_forward_id: int, session: AsyncSession | None = None, *, retry_policy: ForwardRetryPolicy | None = None
) -> bool:
    retry_policy = retry_policy or ForwardRetryPolicy.from_config()

    async def _reset(sess: AsyncSession) -> bool:
        record = await sess.get(FailedForward, failed_forward_id)
        if not record or record.status != FailedForwardStatus.EXHAUSTED:
            return False
        now = datetime.now()
        record.status, record.retry_count, record.updated_at = FailedForwardStatus.PENDING, 0, now
        record.next_retry_at = now + timedelta(seconds=retry_policy.initial_delay)
        await sess.flush()
        await _schedule_failed_forward_retry(record.id, retry_policy.initial_delay, policy=retry_policy)
        logger.info("[ForwardRetry] 手动重置失败转发 id=%s event_id=%s", record.id, record.webhook_event_id)
        return True

    return await _with_session(session, _reset)


async def delete_failed_forward(failed_forward_id: int, session: AsyncSession | None = None) -> bool:
    async def _delete(sess: AsyncSession) -> bool:
        record = await sess.get(FailedForward, failed_forward_id)
        if not record:
            return False
        await sess.delete(record)
        await sess.flush()
        return True

    return await _with_session(session, _delete)


async def cleanup_old_success_records(days: int = 7, session: AsyncSession | None = None) -> int:
    cutoff = datetime.now() - timedelta(days=days)

    async def _cleanup(sess: AsyncSession) -> int:
        stmt = (
            sa_delete(FailedForward)
            .where(FailedForward.status == FailedForwardStatus.SUCCESS)
            .where(FailedForward.updated_at < cutoff)
        )
        result = await sess.execute(stmt)
        count = int(result.rowcount or 0)
        await sess.flush()
        return count

    return await _with_session(session, _cleanup)

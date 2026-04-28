"""
转发失败重试 Poller — 后台指数退避重试

定期扫描 failed_forwards 表中待重试的记录，
使用指数退避策略逐条调用 forward_to_remote() 进行重试。
"""

import asyncio
import logging
import threading
from datetime import datetime, timedelta

from sqlalchemy import select

from core.config import Config
from db.session import session_scope
from models import FailedForward, WebhookEvent
from services.pollers import _stop_event

logger = logging.getLogger("webhook_service.forward_retry")


async def run_forward_retry_poller():
    """主循环：每 FORWARD_RETRY_POLL_INTERVAL 秒执行一次 poll_pending_retries()"""
    logger.info("[ForwardRetry] 转发重试 Poller 已启动")

    while not _stop_event.is_set():
        try:
            await poll_pending_retries()
        except Exception as e:
            logger.error(f"[ForwardRetry] poll_pending_retries 异常: {e}")

        # 使用 _stop_event.wait() 替代 asyncio.sleep，以便优雅停机时快速退出
        for _ in range(Config.FORWARD_RETRY_POLL_INTERVAL):
            if _stop_event.is_set():
                break
            await asyncio.sleep(1)

    logger.info("[ForwardRetry] 转发重试 Poller 已停止")


async def poll_pending_retries():
    """核心逻辑：获取 Redis 分布式锁，查询待重试记录，逐条重试"""
    from core.redis_client import get_redis

    redis = get_redis()
    lock_key = "forward:retry:poller:lock"

    # 获取分布式锁，有效期 60 秒
    acquired = await redis.set(lock_key, "1", nx=True, ex=60)
    if not acquired:
        logger.debug("[ForwardRetry] 未获取到分布式锁，跳过本轮")
        return

    try:
        now = datetime.now()

        async with session_scope() as session:
            # 查询待重试记录：status IN ('pending', 'retrying') AND next_retry_at <= now
            stmt = (
                select(FailedForward)
                .filter(
                    FailedForward.status.in_(["pending", "retrying"]),
                    FailedForward.next_retry_at <= now,
                )
                .order_by(FailedForward.next_retry_at.asc())
                .limit(Config.FORWARD_RETRY_BATCH_SIZE)
            )
            result = await session.execute(stmt)
            records = result.scalars().all()

            if not records:
                return

            logger.info(f"[ForwardRetry] 本轮扫描到 {len(records)} 条待重试记录")

            for record in records:
                try:
                    await _retry_forward(session, record)
                except Exception as e:  # noqa: PERF203
                    logger.error(
                        f"[ForwardRetry] 重试记录 ID={record.id} 异常: {e}"
                    )

    finally:
        # 释放锁
        import contextlib
        with contextlib.suppress(Exception):
            await redis.delete(lock_key)


async def _retry_forward(session, record: FailedForward):
    """重试单条转发失败记录"""
    from services.ai_analyzer import forward_to_remote

    # 从 DB 获取关联的 WebhookEvent（获取 ai_analysis 等信息）
    event = await session.get(WebhookEvent, record.webhook_event_id)
    if not event:
        logger.warning(
            f"[ForwardRetry] 关联事件不存在: webhook_event_id={record.webhook_event_id}, "
            f"标记为 exhausted"
        )
        record.status = "exhausted"
        record.updated_at = datetime.now()
        await session.flush()
        return

    # 构建 webhook_data 和 analysis_result
    webhook_data = {
        "parsed_data": event.parsed_data or {},
        "source": event.source,
        "timestamp": event.timestamp.isoformat() if event.timestamp else None,
        "client_ip": event.client_ip,
    }
    analysis_result = event.ai_analysis or {}

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
            logger.info(
                f"[ForwardRetry] 重试成功: ID={record.id}, "
                f"webhook_event_id={record.webhook_event_id}"
            )
        else:
            _handle_retry_failure(
                record, now, f"forward status={status}: {result.get('message', '')}"
            )

    except Exception as e:
        _handle_retry_failure(record, now, str(e))

    await session.flush()


def _handle_retry_failure(record: FailedForward, now: datetime, error_msg: str):
    """处理重试失败：更新计数、计算下次重试时间或标记为 exhausted"""
    record.retry_count += 1
    record.last_retry_at = now
    record.error_message = error_msg
    record.updated_at = now

    if record.retry_count >= record.max_retries:
        record.status = "exhausted"
        logger.warning(
            f"[ForwardRetry] 重试次数已耗尽: ID={record.id}, "
            f"retry_count={record.retry_count}/{record.max_retries}"
        )
    else:
        record.status = "retrying"
        # 指数退避：min(initial_delay * multiplier^(retry_count-1), max_delay)
        delay = min(
            Config.FORWARD_RETRY_INITIAL_DELAY
            * Config.FORWARD_RETRY_BACKOFF_MULTIPLIER ** (record.retry_count - 1),
            Config.FORWARD_RETRY_MAX_DELAY,
        )
        record.next_retry_at = now + timedelta(seconds=delay)
        logger.info(
            f"[ForwardRetry] 记录 ID={record.id} 将在 {delay:.0f}s 后重试 "
            f"(retry_count={record.retry_count}/{record.max_retries})"
        )


# ── 线程启动 ──


def _run_poller():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(run_forward_retry_poller())
    finally:
        loop.close()


def start_forward_retry_poller():
    """启动转发重试 Poller 线程"""
    t = threading.Thread(
        target=_run_poller, daemon=True, name="forward-retry-poller"
    )
    t.start()
    logger.info("[ForwardRetry] 转发重试 Poller 线程已启动")
    return t

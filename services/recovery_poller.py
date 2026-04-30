"""RecoveryPoller — 补偿轮询器（Outbox Pattern 兆底补偿）。

周期性扫描数据库中长期处于 received/analyzing 状态的僵尸事件，
以平缓速率重新分发处理，实现削峰填谷和 At-Least-Once 语义。

── Outbox Pattern 兆底补偿说明 ──
Webhook 接收端（api/webhook.py）的两步操作：
  1. session.commit()  → 将事件写入 DB（processing_status='received'）
  2. redis.xadd()      → 投递到 Redis Stream 触发 Worker 处理
若步骤 2 失败（Redis 不可用、网络闪断等），事件将永远停留在 'received' 状态。
RecoveryPoller 正是这一场景的兆底补偿：扫描 processing_status IN ('received', 'analyzing', 'failed')
且 created_at 超过阈值的记录，重新触发 pipeline 处理，确保没有事件被遗漏。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import platform
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select

from core.config import Config
from core.distributed_lock import DistributedLock
from core.logger import get_logger
from core.metrics import WEBHOOK_RECOVERY_POLLED_TOTAL
from db.session import session_scope
from models import WebhookEvent

logger = get_logger("recovery_poller")

# 每次最多处理的僵尸事件数量
_MAX_RECOVER_BATCH = 50

# 最大重试次数，超过后标记为 dead_letter 不再捕捉
_MAX_RETRIES = 5

# Redis 分布式锁
_LOCK_KEY = "recovery:poller:lock"
_LOCK_TTL_SECONDS = 120


def _generate_lock_value() -> str:
    """生成唯一的锁标识值。"""
    hostname = platform.node()
    return f"{hostname}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class RecoveryPoller:
    """补偿轮询器，扫描僵尸事件并重新分发。"""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        """启动轮询循环。"""
        self._stop_event.clear()
        self._task = asyncio.create_task(self._poll_loop())
        logger.info(
            "RecoveryPoller 已启动 | interval=%ds | threshold=%ds",
            Config.server.RECOVERY_POLLER_INTERVAL_SECONDS,
            Config.server.RECOVERY_POLLER_STUCK_THRESHOLD_SECONDS,
        )

    async def stop(self) -> None:
        """停止轮询。"""
        self._stop_event.set()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        logger.info("RecoveryPoller 已停止")

    async def _poll_loop(self) -> None:
        """主轮询循环。"""
        while not self._stop_event.is_set():
            try:
                await self._scan_and_recover()
            except Exception:
                logger.exception("RecoveryPoller 扫描异常")
            # 等待下次扫描或停止信号
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=Config.server.RECOVERY_POLLER_INTERVAL_SECONDS,
                )
                break  # stop_event 被设置，退出循环
            except asyncio.TimeoutError:
                continue  # 超时说明还没停止，继续下一轮

    async def _scan_and_recover(self) -> None:
        """扫描僵尸事件并重新分发。

        使用 Redis NX 分布式锁确保多 Worker 下仅一个实例执行。
        内置 Watchdog 自动续期机制，防止长时间扫描导致锁过期。
        """
        lock_value = _generate_lock_value()
        lock = DistributedLock(key=_LOCK_KEY, ttl=_LOCK_TTL_SECONDS, lock_value=lock_value)

        async with lock as acquired:
            if not acquired:
                logger.debug("[Recovery] 另一个 worker 正在执行，跳过本轮")
                return
            await self._do_recover()

    async def _do_recover(self) -> None:
        """实际恢复逻辑：查找僵尸事件并重新处理。

        Outbox Pattern 兆底补偿：扫描 processing_status IN ('received', 'analyzing', 'failed')
        且 created_at 超过阻塞阈值的记录。这覆盖了以下场景：
        - DB 已写入但 Redis xadd 失败（事件停留在 'received'）
        - Worker 处理中崩溃（事件停留在 'analyzing'）
        - 可重试异常后被 pipeline 退回 'received' 等待重试
        """
        threshold = datetime.now() - timedelta(
            seconds=Config.server.RECOVERY_POLLER_STUCK_THRESHOLD_SECONDS,
        )

        async with session_scope() as session:
            result = await session.execute(
                select(WebhookEvent)
                .where(WebhookEvent.processing_status.in_(["received", "analyzing", "failed"]))
                .where(WebhookEvent.retry_count < _MAX_RETRIES)
                .where(WebhookEvent.created_at < threshold)
                .limit(_MAX_RECOVER_BATCH)
            )
            zombie_events = result.scalars().all()

        # dead_letter 判定已收敛到 pipeline.py 事务闭环内，此处无需再批量扫描

        if not zombie_events:
            return

        logger.info("[Recovery] 发现 %d 条僵尸事件，开始恢复处理", len(zombie_events))

        for event in zombie_events:
            try:
                await self._reprocess_event(event)
            except Exception:  # noqa: PERF203
                logger.exception("[Recovery] 恢复事件 %s 失败", event.id)

    @staticmethod
    async def _reprocess_event(event: WebhookEvent) -> None:
        """重新处理单条僵尸事件。"""
        from services.pipeline import handle_webhook_process

        source = event.source or "unknown"
        logger.info("[Recovery] 重新处理事件 id=%s, source=%s", event.id, source)
        WEBHOOK_RECOVERY_POLLED_TOTAL.inc()

        await handle_webhook_process(
            event_id=event.id,
            client_ip="recovery",
        )

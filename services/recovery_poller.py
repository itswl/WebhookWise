"""RecoveryPoller — 补偿轮询器。

周期性扫描数据库中长期处于 received/analyzing 状态的僵尸事件，
以平缓速率重新分发处理，实现削峰填谷和 At-Least-Once 语义。
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select

from core.config import Config
from core.logger import get_logger
from core.metrics import WEBHOOK_RECOVERY_POLLED_TOTAL
from core.redis_client import get_redis
from db.session import session_scope
from models import WebhookEvent

logger = get_logger("recovery_poller")

# 每次最多处理的僵尸事件数量
_MAX_RECOVER_BATCH = 50

# Redis 分布式锁
_LOCK_KEY = "recovery:poller:lock"
_LOCK_TTL_SECONDS = 120

_RELEASE_LOCK_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""


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
        """
        redis = get_redis()
        lock_value = str(uuid.uuid4())
        if not await redis.set(_LOCK_KEY, lock_value, nx=True, ex=_LOCK_TTL_SECONDS):
            logger.debug("[Recovery] 另一个 worker 正在执行，跳过本轮")
            return

        try:
            await self._do_recover()
        finally:
            with contextlib.suppress(Exception):
                await redis.eval(_RELEASE_LOCK_LUA, 1, _LOCK_KEY, lock_value)

    async def _do_recover(self) -> None:
        """实际恢复逻辑：查找僵尸事件并重新处理。"""
        threshold = datetime.now() - timedelta(
            seconds=Config.server.RECOVERY_POLLER_STUCK_THRESHOLD_SECONDS,
        )

        async with session_scope() as session:
            result = await session.execute(
                select(WebhookEvent)
                .where(WebhookEvent.processing_status.in_(["received", "analyzing"]))
                .where(WebhookEvent.created_at < threshold)
                .limit(_MAX_RECOVER_BATCH)
            )
            zombie_events = result.scalars().all()

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

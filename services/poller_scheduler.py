"""统一异步调度器 — 所有 poller 在 FastAPI 主事件循环中以 asyncio.Task 运行

每个 Worker 均启动全部 Poller 定时循环，各 Poller 内部通过 Redis NX 分布式锁自行互斥。
"""

import asyncio
import logging
from collections.abc import Callable, Coroutine

from core.config import Config

logger = logging.getLogger(__name__)

_stop_event: asyncio.Event | None = None
_tasks: list[asyncio.Task] = []


# ── 通用定时执行包装器 ──


async def _run_periodic(
    name: str,
    coro_fn: Callable[[], Coroutine],
    interval: float,
    stop_event: asyncio.Event,
):
    """每 interval 秒执行一次 coro_fn，支持优雅停止"""
    while not stop_event.is_set():
        try:
            await coro_fn()
        except Exception:
            logger.exception(f"[{name}] 轮询执行异常，将在 {interval}s 后重试")
        # 可中断等待
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            break  # stop_event was set
        except asyncio.TimeoutError:
            pass  # 正常超时，继续下一轮


def _on_task_done(task: asyncio.Task) -> None:
    """Task 完成回调：记录非正常退出"""
    if task.cancelled():
        logger.debug(f"[Scheduler] task {task.get_name()} 已取消")
    elif exc := task.exception():
        logger.error(f"[Scheduler] task {task.get_name()} 异常退出: {exc}", exc_info=exc)


# ── 启动 / 停止 ──


async def start_scheduler() -> bool:
    """启动所有轮询任务（在主事件循环中以 asyncio.Task 运行）

    所有 Worker 均会启动调度循环，各 Poller 内部通过 Redis NX 锁自行互斥。
    """
    global _stop_event, _tasks

    if not Config.server.ENABLE_POLLERS:
        logger.info("[Pollers] disabled by config")
        return False

    _stop_event = asyncio.Event()
    _tasks = []

    def _create_task(coro, name: str):
        t = asyncio.create_task(coro, name=name)
        t.add_done_callback(_on_task_done)
        _tasks.append(t)

    # 1. Maintenance poller（每 600s）
    try:
        from services.maintenance_poller import check_and_run_maintenance

        _create_task(
            _run_periodic("Maintenance", check_and_run_maintenance, 600, _stop_event),
            "maintenance-poller",
        )
    except Exception as e:
        logger.warning(f"[Pollers] maintenance poller start failed: {e}")

    # 2. OpenClaw poller（每 30s）
    try:
        from services.openclaw_poller import poll_pending_analyses

        _create_task(
            _run_periodic("OpenClaw", poll_pending_analyses, 30, _stop_event),
            "openclaw-poller",
        )
    except Exception as e:
        logger.warning(f"[Pollers] openclaw poller start failed: {e}")

    # 3. Recovery poller — 僵尸事件恢复（每 120s）
    try:
        from services.recovery_poller import recover_zombie_events

        _create_task(
            _run_periodic("Recovery", recover_zombie_events, 120, _stop_event),
            "recovery-poller",
        )
    except Exception as e:
        logger.warning(f"[Pollers] recovery poller start failed: {e}")

    # 4. Forward retry poller（按配置启用）
    if Config.retry.ENABLE_FORWARD_RETRY:
        try:
            from services.forward_retry_poller import poll_pending_retries

            interval = Config.retry.FORWARD_RETRY_POLL_INTERVAL
            _create_task(
                _run_periodic("ForwardRetry", poll_pending_retries, interval, _stop_event),
                "forward-retry-poller",
            )
        except Exception as e:
            logger.warning(f"[Pollers] forward retry poller start failed: {e}")

    logger.info("[Pollers] started")
    return True


async def stop_scheduler():
    """优雅停止所有轮询任务"""
    global _stop_event, _tasks

    if _stop_event:
        _stop_event.set()

    if _tasks:
        done, pending = await asyncio.wait(_tasks, timeout=10)
        for t in pending:
            t.cancel()
        # 等待被取消的 task 完成清理
        if pending:
            await asyncio.wait(pending, timeout=3)

    _tasks = []
    logger.info("[Pollers] stopped")

"""统一异步调度器 — 所有 poller 在 FastAPI 主事件循环中以 asyncio.Task 运行"""

import asyncio
import logging
import os
from collections.abc import Callable, Coroutine

from core.config import Config
from core.redis_client import get_redis

logger = logging.getLogger(__name__)

_stop_event: asyncio.Event | None = None
_tasks: list[asyncio.Task] = []

_LEADER_KEY = "pollers:leader"
_LEADER_TTL_SECONDS = 90
_RENEW_INTERVAL_SECONDS = 30


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


# ── Leader 续期 ──


async def _renew_leader(token: str, stop_event: asyncio.Event) -> None:
    """定期续期 Redis leader 锁"""
    while not stop_event.is_set():
        try:
            redis = get_redis()
            current = await redis.get(_LEADER_KEY)
            if (
                current is None
                or (isinstance(current, bytes) and current.decode("utf-8") != token)
                or (isinstance(current, str) and current != token)
            ):
                logger.warning("[Pollers] leader 锁已被其他 worker 接管，停止续期")
                return
            await redis.expire(_LEADER_KEY, _LEADER_TTL_SECONDS)
        except Exception as e:
            logger.warning(f"[Pollers] leader renew failed: {e}")
            return
        # 可中断等待
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=_RENEW_INTERVAL_SECONDS)
            break
        except asyncio.TimeoutError:
            pass


def _on_task_done(task: asyncio.Task) -> None:
    """Task 完成回调：记录非正常退出"""
    if task.cancelled():
        logger.debug(f"[Scheduler] task {task.get_name()} 已取消")
    elif exc := task.exception():
        logger.error(f"[Scheduler] task {task.get_name()} 异常退出: {exc}", exc_info=exc)


# ── 启动 / 停止 ──


async def start_scheduler(worker_id: str | None = None) -> bool:
    """启动所有轮询任务（在主事件循环中以 asyncio.Task 运行）"""
    global _stop_event, _tasks

    if not getattr(Config, "ENABLE_POLLERS", True):
        logger.info("[Pollers] disabled by config")
        return False

    worker_id = worker_id or f"{os.getpid()}"

    # 1. 获取 Redis leader 锁
    try:
        redis = get_redis()
    except Exception as e:
        logger.warning(f"[Pollers] redis unavailable, skip starting pollers: {e}")
        return False

    try:
        acquired = await redis.set(_LEADER_KEY, worker_id, nx=True, ex=_LEADER_TTL_SECONDS)
    except Exception as e:
        logger.warning(f"[Pollers] failed to acquire leader lock: {e}")
        return False

    if not acquired:
        logger.info("[Pollers] leader exists, skip starting pollers")
        return False

    _stop_event = asyncio.Event()
    _tasks = []

    def _create_task(coro, name: str):
        t = asyncio.create_task(coro, name=name)
        t.add_done_callback(_on_task_done)
        _tasks.append(t)

    # 2. Leader 续期
    _create_task(_renew_leader(worker_id, _stop_event), "pollers-leader-renew")

    # 3. Maintenance poller（无条件启动）
    try:
        from services.maintenance_poller import check_and_run_maintenance

        _create_task(
            _run_periodic("Maintenance", check_and_run_maintenance, 600, _stop_event),
            "maintenance-poller",
        )
    except Exception as e:
        logger.warning(f"[Pollers] maintenance poller start failed: {e}")

    # 4. OpenClaw poller
    try:
        from services.openclaw_poller import poll_pending_analyses

        _create_task(
            _run_periodic("OpenClaw", poll_pending_analyses, 30, _stop_event),
            "openclaw-poller",
        )
    except Exception as e:
        logger.warning(f"[Pollers] openclaw poller start failed: {e}")

    # 5. Forward retry poller（按配置启用）
    if Config.ENABLE_FORWARD_RETRY:
        try:
            from services.forward_retry_poller import poll_pending_retries

            interval = Config.FORWARD_RETRY_POLL_INTERVAL
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

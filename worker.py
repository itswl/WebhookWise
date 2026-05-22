"""独立 Worker 进程入口 - 运行 TaskIQ Worker"""

from __future__ import annotations

import asyncio
import os
import signal

import uvloop

os.environ.setdefault("RUN_MODE", "worker")

from core.app_context import AppContext, get_or_create_default_app_context, set_default_app_context
from core.config import UnifiedConfigManager
from core.logger import get_logger, stop_log_listener
from services.operations.taskiq_wiring import broker

asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
logger = get_logger("worker")


async def startup() -> None:
    """初始化工作进程环境"""
    logger.info("[Worker] 正在初始化工作进程...")
    context = AppContext(config=UnifiedConfigManager())
    set_default_app_context(context)
    await broker.startup()
    logger.info("[Worker] TaskIQ Broker 已启动")


async def shutdown() -> None:
    """清理工作进程环境"""
    logger.info("[Worker] 正在关闭工作进程...")
    get_or_create_default_app_context()
    await broker.shutdown()
    logger.info("[Worker] 关闭完成。")
    stop_log_listener()


async def run_worker() -> None:
    """启动 TaskIQ Worker 进程（仅用于编程式启动，生产推荐使用 entrypoint.sh）"""
    await startup()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: stop_event.set())

    await stop_event.wait()
    await shutdown()


if __name__ == "__main__":
    asyncio.run(run_worker())

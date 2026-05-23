"""独立 Worker 进程入口 - 运行 TaskIQ Worker"""

from __future__ import annotations

import asyncio
import os
import signal

import uvloop

os.environ.setdefault("RUN_MODE", "worker")

from core.app_context import get_default_app_context, init_default_app_context
from core.config import UnifiedConfigManager
from core.logger import get_logger, setup_logger, stop_log_listener
from core.observability import setup_observability_worker, shutdown_observability
from core.service_lifecycle import start_runtime_services, stop_runtime_services
from services.operations.taskiq_wiring import broker

asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
logger = get_logger("worker")


async def startup() -> None:
    """初始化工作进程环境"""
    logger.info("[Worker] 正在初始化工作进程...")
    config = UnifiedConfigManager()
    context = init_default_app_context(config)
    await start_runtime_services(
        config,
        context=context,
        broker=broker,
        start_broker=True,
        initialize_logger=setup_logger,
        initialize_observability=setup_observability_worker,
        initialize_redis_client=True,
        initialize_ai_client=True,
    )
    logger.info("[Worker] runtime 已启动")


async def shutdown() -> None:
    """清理工作进程环境"""
    logger.info("[Worker] 正在关闭工作进程...")
    context = get_default_app_context()
    config = context.config if context is not None else UnifiedConfigManager()
    await stop_runtime_services(
        config,
        context=context,
        broker=broker,
        stop_broker=True,
        reset_ai_client=True,
    )
    shutdown_observability()
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

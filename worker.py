"""独立 Worker 进程入口 - 运行 TaskIQ Worker"""

import asyncio
import logging
import signal

import uvloop

from core.config import Config
from core.logger import setup_logger, stop_log_listener
from core.observability import setup_observability_worker
from core.service_lifecycle import start_runtime_services, stop_runtime_services
from services.operations.taskiq_wiring import broker

asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
logger = logging.getLogger("webhook_service.worker")


async def startup() -> None:
    """初始化工作进程环境"""
    logger.info("[Worker] 正在初始化工作进程...")
    await start_runtime_services(
        Config,
        broker=broker,
        start_broker=True,
        initialize_logger=setup_logger,
        initialize_observability=setup_observability_worker,
        initialize_redis_client=True,
        initialize_ai_client=True,
    )
    logger.info("[Worker] TaskIQ Broker 已启动")


async def shutdown() -> None:
    """清理工作进程环境"""
    logger.info("[Worker] 正在关闭工作进程...")
    await stop_runtime_services(
        Config,
        broker=broker,
        stop_broker=True,
        reset_ai_client=True,
    )
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

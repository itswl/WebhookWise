"""独立 Worker 进程入口 - 运行 TaskIQ Worker"""

import asyncio
import logging
import signal

import uvloop

# 确保导入任务，以便 TaskIQ 注册
import services.tasks  # noqa: F401
from core.config import Config
from core.http_client import close_http_client, get_http_client
from core.logger import setup_logger, stop_log_listener
from core.redis_client import dispose_redis, get_redis
from core.taskiq_broker import broker
from db.session import dispose_engine, init_engine

asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
logger = logging.getLogger("webhook_service.worker")


async def startup():
    """初始化工作进程环境"""
    setup_logger()
    logger.info("[Worker] 正在初始化工作进程...")

    get_http_client()
    await init_engine()
    get_redis()

    await Config.load_from_db()
    await Config.start_subscriber()

    await broker.startup()
    logger.info("[Worker] TaskIQ Broker 已启动")


async def shutdown():
    """清理工作进程环境"""
    logger.info("[Worker] 正在关闭工作进程...")
    await broker.shutdown()
    await Config.stop_subscriber()
    await dispose_engine()
    await dispose_redis()
    await close_http_client()
    stop_log_listener()
    logger.info("[Worker] 关闭完成。")


async def run_worker():
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

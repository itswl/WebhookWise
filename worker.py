"""独立 Worker 进程入口 - 仅运行 Poller 调度器"""

import asyncio
import signal

from core.config import Config
from core.http_client import close_http_client, get_http_client
from core.logger import setup_logger, stop_log_listener
from core.redis_client import dispose_redis, get_redis
from core.runtime_config import runtime_config
from db.session import dispose_engine, init_engine


async def main():
    logger = setup_logger()
    logger.info("[Worker] Starting poller worker...")

    # 初始化依赖（与 lifespan 保持一致）
    Config.validate_config()
    get_http_client()  # Poller 需要 HTTP 客户端（如 OpenClaw 轮询）
    await init_engine()
    get_redis()  # 确保 Redis 连接就绪

    # 从数据库加载运行时配置
    await runtime_config.load_from_db()
    await runtime_config.start_subscriber()

    from services.poller_scheduler import start_scheduler, stop_scheduler

    stop_event = asyncio.Event()

    def _signal_handler():
        logger.info("[Worker] Received shutdown signal")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _signal_handler)

    await start_scheduler()
    logger.info("[Worker] Poller worker started, waiting for shutdown signal...")
    await stop_event.wait()

    logger.info("[Worker] Shutting down...")
    await stop_scheduler()
    await runtime_config.stop_subscriber()
    await dispose_engine()
    await dispose_redis()
    await close_http_client()
    stop_log_listener()
    logger.info("[Worker] Shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())

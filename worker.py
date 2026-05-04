"""独立 Worker 进程入口 - 运行 TaskIQ Worker 和 Scheduler"""

import asyncio
import logging
import signal
from typing import Any

import uvloop
from taskiq import AsyncBroker
from taskiq.receiver import Receiver
from taskiq.schedule_sources import LabelScheduleSource

from core.http_client import close_http_client, get_http_client
from core.logger import setup_logger, stop_log_listener
from core.redis_client import dispose_redis, get_redis
from core.runtime_config import runtime_config
from core.taskiq_broker import broker, schedule_source
from db.session import dispose_engine, init_engine

# 确保导入任务，以便 TaskIQ 注册
import services.tasks  # noqa: F401

asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
logger = logging.getLogger("webhook_service.worker")


async def startup():
    """初始化工作进程环境"""
    setup_logger()
    logger.info("[Worker] 正在初始化工作进程...")

    # 初始化依赖
    get_http_client()
    await init_engine()
    get_redis()

    # 加载运行时配置
    await runtime_config.load_from_db()
    await runtime_config.start_subscriber()

    # 启动 TaskIQ 运行环境
    await broker.startup()
    logger.info("[Worker] TaskIQ Broker 已启动")


async def shutdown():
    """清理工作进程环境"""
    logger.info("[Worker] 正在关闭工作进程...")
    await broker.shutdown()
    await runtime_config.stop_subscriber()
    await dispose_engine()
    await dispose_redis()
    await close_http_client()
    stop_log_listener()
    logger.info("[Worker] 关闭完成。")


async def run_worker():
    """启动 TaskIQ Worker 进程"""
    await startup()
    
    # 注册定时任务 (Scheduler 逻辑)
    # 注意：在生产环境下，建议 Scheduler 作为一个单独的进程启动
    # 这里为了演示方便，我们在 Worker 启动时同步配置调度
    
    from core.config import Config
    
    # 1. 每日维护 (凌晨 3 点)
    await broker.task("maintenance_task").schedule(
        schedule_source,
        cron=f"0 {Config.maintenance.MAINTENANCE_HOUR} * * *",
    )
    
    # 2. 僵尸事件恢复 (每 60s)
    await broker.task("recovery_task").schedule(
        schedule_source,
        interval=Config.server.RECOVERY_POLLER_INTERVAL_SECONDS,
    )
    
    # 3. 转发重试 (每 30s)
    if Config.retry.ENABLE_FORWARD_RETRY:
        await broker.task("forward_retry_task").schedule(
            schedule_source,
            interval=Config.retry.FORWARD_RETRY_POLL_INTERVAL,
        )
        
    # 4. OpenClaw 轮询 (每 30s)
    await broker.task("openclaw_poll_task").schedule(
        schedule_source,
        interval=30,
    )

    # 5. 指标刷新 (每 15s)
    await broker.task("metrics_refresh_task").schedule(
        schedule_source,
        interval=15,
    )

    logger.info("[Worker] 正在运行任务消费者...")
    # 注意：这里我们通常直接运行 'taskiq worker core.taskiq_broker:broker' 命令行
    # 如果要编程式启动，可以调用 Receiver
    
    # 此处我们维持现有的 signal 等待逻辑，让命令行来驱动真正的 worker 运行
    # 或者我们可以在这里通过 Receiver 手动运行 (较复杂)
    # 推荐做法：修改 entrypoint.sh 使用 taskiq 命令行
    
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: stop_event.set())
        
    await stop_event.wait()
    await shutdown()


if __name__ == "__main__":
    # 为了简化，我们建议用户使用命令行启动：
    # taskiq worker core.taskiq_broker:broker --fs-import services.tasks
    # taskiq scheduler core.taskiq_broker:broker core.taskiq_broker:schedule_source
    
    # 这里仅保留一个基础的启动入口
    asyncio.run(run_worker())

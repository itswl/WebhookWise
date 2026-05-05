"""TaskIQ Broker 配置

定义异步任务代理，支持定时任务 (Schedule) 和分布式执行。
"""

import asyncio
import logging

from taskiq import InMemoryBroker
from taskiq.scheduler.scheduler import TaskiqScheduler
from taskiq_redis import ListQueueBroker, RedisAsyncResultBackend, RedisScheduleSource

from core.config import Config

logger = logging.getLogger("webhook_service.taskiq")

# Redis 连接配置
REDIS_URL = Config.redis.REDIS_URL

# 1. 结果后端 (用于获取任务返回值)
result_backend = RedisAsyncResultBackend(
    redis_url=REDIS_URL,
)

# 2. 调度器源 (用于管理定时任务)
schedule_source = RedisScheduleSource(
    url=REDIS_URL,
)

# 3. 异步任务代理
broker = ListQueueBroker(
    url=REDIS_URL,
).with_result_backend(result_backend)

# 4. 调度器对象
scheduler = TaskiqScheduler(
    broker=broker,
    sources=[schedule_source],
)

# 在测试环境下可以切换为 InMemoryBroker
if Config.server.DEBUG and not REDIS_URL.startswith("redis"):
    broker = InMemoryBroker()
    logger.info("[TaskIQ] 使用 InMemoryBroker (DEBUG 模式)")
else:
    logger.info("[TaskIQ] 已初始化 Redis Broker: %s", REDIS_URL)

_poller_task: asyncio.Task | None = None


async def _run_openclaw_poller_loop():
    """内置 openclaw poller 循环，每 30 秒执行一次，不依赖外部 scheduler 进程。"""
    from services.openclaw_poller import poll_pending_analyses
    while True:
        try:
            await poll_pending_analyses()
        except Exception as e:
            logger.error("[TaskIQ] openclaw poller 异常: %s", e)
        await asyncio.sleep(30)


@broker.on_event("startup")
async def startup_event():
    """Worker 启动时的生命周期事件"""
    global _poller_task
    from core.config import Config
    from core.http_client import get_http_client
    from core.logger import setup_logger
    from db.session import init_engine

    # 确保日志系统已初始化（taskiq CLI 不走 worker.py::startup）
    setup_logger()
    # 确保数据库已初始化
    await init_engine()
    # 确保配置和 HTTP 客户端已初始化
    get_http_client()
    await Config.load_from_db()
    await Config.start_subscriber()
    # 注册定时任务（幂等，每次 worker 启动时覆盖写入）
    try:
        from worker import _register_schedules
        await _register_schedules()
    except Exception as _e:
        logger.warning("[TaskIQ] 定时任务注册失败: %s", _e)
    # 启动内置 openclaw poller 循环（不依赖外部 scheduler 进程）
    _poller_task = asyncio.create_task(_run_openclaw_poller_loop())
    logger.info("[TaskIQ] openclaw poller 后台循环已启动")
    # 启动时立即执行一次 recovery，捞起重启前遗留的僵尸事件（不受阈值限制）
    try:
        from services.recovery_poller import run_recovery_scan
        await run_recovery_scan(stuck_threshold_seconds=0)
        logger.info("[TaskIQ] 启动恢复扫描完成")
    except Exception as _e:
        logger.warning("[TaskIQ] 启动恢复扫描失败: %s", _e)


@broker.on_event("shutdown")
async def shutdown_event():
    """Worker 关闭时的生命周期事件"""
    global _poller_task
    from core.config import Config
    from core.http_client import close_http_client
    from db.session import dispose_engine

    if _poller_task:
        _poller_task.cancel()
        _poller_task = None

    await Config.stop_subscriber()
    await dispose_engine()
    await close_http_client()

import contextlib
import logging
import threading
from datetime import datetime

from services.data_maintenance import archive_old_data
from services.pollers import _stop_event

logger = logging.getLogger("webhook_service.maintenance")


async def run_daily_maintenance():
    """每日维护任务：归档旧数据"""
    last_run_date = None

    logger.info("[Maintenance] 每日维护轮询已启动")

    while not _stop_event.is_set():
        try:
            now = datetime.now()
            # 每天凌晨 3 点执行
            if now.hour == 3 and now.date() != last_run_date:
                logger.info(f"[Maintenance] 开始执行凌晨维护任务 (当前时间: {now.strftime('%H:%M:%S')})")

                # 1. 归档 30 天前的数据
                moved = await archive_old_data(archive_days=30)
                logger.info(f"[Maintenance] 归档任务完成，移动了 {moved} 条记录")

                last_run_date = now.date()

        except Exception as e:
            logger.error(f"[Maintenance] 维护任务异常: {e}")

        # 每 10 分钟检查一次时间
        await __import__("asyncio").sleep(600)


async def _dispose_poller_resources():
    """显式清理当前事件循环的数据库引擎和 Redis 连接"""
    from core.redis_client import dispose_redis
    from db.session import dispose_engine

    await dispose_engine()
    await dispose_redis()


def _run_poller():
    import asyncio

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(run_daily_maintenance())
    finally:
        # 显式清理当前循环的连接资源
        with contextlib.suppress(Exception):
            loop.run_until_complete(_dispose_poller_resources())
        loop.close()


def start_maintenance_poller():
    """启动维护线程"""
    t = threading.Thread(target=_run_poller, daemon=True, name="maintenance-poller")
    t.start()
    return t

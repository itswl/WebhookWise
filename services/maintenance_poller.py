import logging
import threading
import time
from datetime import datetime

from services.data_maintenance import archive_old_data

logger = logging.getLogger('webhook_service.maintenance')

def run_daily_maintenance():
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
                moved = archive_old_data(days=30)
                logger.info(f"[Maintenance] 归档任务完成，移动了 {moved} 条记录")

                last_run_date = now.date()

        except Exception as e:
            logger.error(f"[Maintenance] 维护任务异常: {e}")

        # 每 10 分钟检查一次时间
        time.sleep(600)

from services.pollers import _stop_event


def start_maintenance_poller():
    """启动维护线程"""
    t = threading.Thread(target=run_daily_maintenance, daemon=True, name='maintenance-poller')
    t.start()
    return t

import logging
from datetime import datetime

from services.data_maintenance import archive_old_data

logger = logging.getLogger("webhook_service.maintenance")

# 记录上次执行日期，避免同一天重复执行
_last_run_date = None


async def check_and_run_maintenance():
    """检查是否到达凌晨 3 点，是则执行每日维护任务（归档旧数据）。

    由 poller_scheduler 每 10 分钟调度一次。
    """
    global _last_run_date

    now = datetime.now()
    if now.hour == 3 and now.date() != _last_run_date:
        logger.info(f"[Maintenance] 开始执行凌晨维护任务 (当前时间: {now.strftime('%H:%M:%S')})")

        # 1. 归档 30 天前的数据
        moved = await archive_old_data(archive_days=30)
        logger.info(f"[Maintenance] 归档任务完成，移动了 {moved} 条记录")

        _last_run_date = now.date()
